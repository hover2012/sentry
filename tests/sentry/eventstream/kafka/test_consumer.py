from __future__ import absolute_import

import subprocess
import uuid
from collections import defaultdict
from contextlib import contextmanager

from confluent_kafka import Consumer, Producer

from sentry.eventstream.kafka.consumer import SynchronizedConsumer


@contextmanager
def create_topic(partitions=1, replication_factor=1):
    command = ['docker', 'exec', 'kafka', 'kafka-topics'] + ['--zookeeper', 'zookeeper:2181']
    topic = 'test-{}'.format(uuid.uuid1().hex)
    subprocess.check_call(command + [
        '--create',
        '--topic', topic,
        '--partitions', '{}'.format(partitions),
        '--replication-factor', '{}'.format(replication_factor),
    ])
    try:
        yield topic
    finally:
        subprocess.check_call(command + [
            '--delete',
            '--topic', topic,
        ])


def test_consumer_start_from_partition_start():
    synchronize_commit_group = 'consumer-{}'.format(uuid.uuid1().hex)

    messages_delivered = defaultdict(list)

    def record_message_delivered(error, message):
        assert error is None
        messages_delivered[message.topic()].append(message)

    producer = Producer({
        'bootstrap.servers': 'localhost:9092',
        'on_delivery': record_message_delivered,
    })

    with create_topic() as topic, create_topic() as commit_log_topic:

        # Produce some messages into the topic.
        for i in range(3):
            producer.produce(topic, '{}'.format(i).encode('utf8'))

        assert producer.flush(5) == 0, 'producer did not successfully flush queue'

        # Create the synchronized consumer.
        consumer = SynchronizedConsumer(
            bootstrap_servers='localhost:9092',
            consumer_group='consumer-{}'.format(uuid.uuid1().hex),
            commit_log_topic=commit_log_topic,
            synchronize_commit_group=synchronize_commit_group,
        )

        assignments_received = []

        def on_assign(c, assignment):
            assert c is consumer
            assignments_received.append(assignment)

        consumer.subscribe([topic], on_assign=on_assign)

        # Wait until we have received our assignments.
        for i in xrange(10):  # this takes a while
            assert consumer.poll(1) is None
            if assignments_received:
                break

        assert len(assignments_received) == 1, 'expected to receive partition assignment'
        assert set((i.topic, i.partition) for i in assignments_received[0]) == set([(topic, 0)])

        # TODO: Make sure that all partitions remain paused.

        # Make sure that there are no messages ready to consume.
        assert consumer.poll(1) is None

        # Move the committed offset forward for our synchronizing group.
        message = messages_delivered[topic][0]
        producer.produce(
            commit_log_topic,
            key='{}:{}:{}'.format(
                message.topic(),
                message.partition(),
                synchronize_commit_group,
            ).encode('utf8'),
            value='{}'.format(
                message.offset() + 1,
            ).encode('utf8'),
        )

        assert producer.flush(5) == 0, 'producer did not successfully flush queue'

        # We should have received a single message.
        # TODO: Can we also assert that the position is unpaused?)
        for i in xrange(5):
            message = consumer.poll(1)
            if message is not None:
                break

        assert message is not None, 'no message received'

        expected_message = messages_delivered[topic][0]
        assert message.topic() == expected_message.topic()
        assert message.partition() == expected_message.partition()
        assert message.offset() == expected_message.offset()

        # We should not be able to continue reading into the topic.
        # TODO: Can we assert that the position is paused?
        assert consumer.poll(1) is None


def test_consumer_start_from_committed_offset():
    consumer_group = 'consumer-{}'.format(uuid.uuid1().hex)
    synchronize_commit_group = 'consumer-{}'.format(uuid.uuid1().hex)

    messages_delivered = defaultdict(list)

    def record_message_delivered(error, message):
        assert error is None
        messages_delivered[message.topic()].append(message)

    producer = Producer({
        'bootstrap.servers': 'localhost:9092',
        'on_delivery': record_message_delivered,
    })

    with create_topic() as topic, create_topic() as commit_log_topic:

        # Produce some messages into the topic.
        for i in range(3):
            producer.produce(topic, '{}'.format(i).encode('utf8'))

        assert producer.flush(5) == 0, 'producer did not successfully flush queue'

        Consumer({
            'bootstrap.servers': 'localhost:9092',
            'group.id': consumer_group,
        }).commit(
            message=messages_delivered[topic][0],
            asynchronous=False,
        )

        # Create the synchronized consumer.
        consumer = SynchronizedConsumer(
            bootstrap_servers='localhost:9092',
            consumer_group=consumer_group,
            commit_log_topic=commit_log_topic,
            synchronize_commit_group=synchronize_commit_group,
        )

        assignments_received = []

        def on_assign(c, assignment):
            assert c is consumer
            assignments_received.append(assignment)

        consumer.subscribe([topic], on_assign=on_assign)

        # Wait until we have received our assignments.
        for i in xrange(10):  # this takes a while
            assert consumer.poll(1) is None
            if assignments_received:
                break

        assert len(assignments_received) == 1, 'expected to receive partition assignment'
        assert set((i.topic, i.partition) for i in assignments_received[0]) == set([(topic, 0)])

        # TODO: Make sure that all partitions are paused on assignment.

        # Move the committed offset forward for our synchronizing group.
        message = messages_delivered[topic][0]
        producer.produce(
            commit_log_topic,
            key='{}:{}:{}'.format(
                message.topic(),
                message.partition(),
                synchronize_commit_group,
            ).encode('utf8'),
            value='{}'.format(
                message.offset() + 1,
            ).encode('utf8'),
        )

        # Make sure that there are no messages ready to consume.
        assert consumer.poll(1) is None

        # Move the committed offset forward for our synchronizing group.
        message = messages_delivered[topic][0 + 1]  # second message
        producer.produce(
            commit_log_topic,
            key='{}:{}:{}'.format(
                message.topic(),
                message.partition(),
                synchronize_commit_group,
            ).encode('utf8'),
            value='{}'.format(
                message.offset() + 1,
            ).encode('utf8'),
        )

        assert producer.flush(5) == 0, 'producer did not successfully flush queue'

        # We should have received a single message.
        # TODO: Can we also assert that the position is unpaused?)
        for i in xrange(5):
            message = consumer.poll(1)
            if message is not None:
                break

        assert message is not None, 'no message received'

        expected_message = messages_delivered[topic][0 + 1]  # second message
        assert message.topic() == expected_message.topic()
        assert message.partition() == expected_message.partition()
        assert message.offset() == expected_message.offset()

        # We should not be able to continue reading into the topic.
        # TODO: Can we assert that the position is paused?
        assert consumer.poll(1) is None


def test_consumer_rebalance_from_partition_start():
    consumer_group = 'consumer-{}'.format(uuid.uuid1().hex)
    synchronize_commit_group = 'consumer-{}'.format(uuid.uuid1().hex)

    messages_delivered = defaultdict(list)

    def record_message_delivered(error, message):
        assert error is None
        messages_delivered[message.topic()].append(message)

    producer = Producer({
        'bootstrap.servers': 'localhost:9092',
        'on_delivery': record_message_delivered,
    })

    with create_topic(partitions=2) as topic, create_topic() as commit_log_topic:

        # Produce some messages into the topic.
        for i in range(4):
            producer.produce(topic, '{}'.format(i).encode('utf8'), partition=i % 2)

        assert producer.flush(5) == 0, 'producer did not successfully flush queue'

        consumer_a = SynchronizedConsumer(
            bootstrap_servers='localhost:9092',
            consumer_group=consumer_group,
            commit_log_topic=commit_log_topic,
            synchronize_commit_group=synchronize_commit_group,
        )

        assignments_received = defaultdict(list)

        def on_assign(consumer, assignment):
            assignments_received[consumer].append(assignment)

        consumer_a.subscribe([topic], on_assign=on_assign)

        # Wait until the first consumer has received its assignments.
        for i in xrange(10):  # this takes a while
            assert consumer_a.poll(1) is None
            if assignments_received[consumer_a]:
                break

        assert len(assignments_received[consumer_a]
                   ) == 1, 'expected to receive partition assignment'
        assert set((i.topic, i.partition)
                   for i in assignments_received[consumer_a][0]) == set([(topic, 0), (topic, 1)])

        assignments_received[consumer_a].pop()

        consumer_b = SynchronizedConsumer(
            bootstrap_servers='localhost:9092',
            consumer_group=consumer_group,
            commit_log_topic=commit_log_topic,
            synchronize_commit_group=synchronize_commit_group,
        )

        consumer_b.subscribe([topic], on_assign=on_assign)

        assignments = {}

        # Wait until *both* consumers have received updated assignments.
        for consumer in [consumer_a, consumer_b]:
            for i in xrange(10):  # this takes a while
                assert consumer.poll(1) is None
                if assignments_received[consumer]:
                    break

            assert len(assignments_received[consumer]
                       ) == 1, 'expected to receive partition assignment'
            assert len(assignments_received[consumer][0]
                       ) == 1, 'expected to have a single partition assignment'

            i = assignments_received[consumer][0][0]
            assignments[(i.topic, i.partition)] = consumer

        assert set(assignments.keys()) == set([(topic, 0), (topic, 1)])

        for expected_message in messages_delivered[topic]:
            consumer = assignments[(expected_message.topic(), expected_message.partition())]

            # Make sure that there are no messages ready to consume.
            assert consumer.poll(1) is None

            # Move the committed offset forward for our synchronizing group.
            producer.produce(
                commit_log_topic,
                key='{}:{}:{}'.format(
                    expected_message.topic(),
                    expected_message.partition(),
                    synchronize_commit_group,
                ).encode('utf8'),
                value='{}'.format(
                    expected_message.offset() + 1,
                ).encode('utf8'),
            )

            assert producer.flush(5) == 0, 'producer did not successfully flush queue'

            # We should have received a single message.
            # TODO: Can we also assert that the position is unpaused?)
            for i in xrange(5):
                received_message = consumer.poll(1)
                if received_message is not None:
                    break

            assert received_message is not None, 'no message received'

            assert received_message.topic() == expected_message.topic()
            assert received_message.partition() == expected_message.partition()
            assert received_message.offset() == expected_message.offset()

            # We should not be able to continue reading into the topic.
            # TODO: Can we assert that the position is paused?
            assert consumer.poll(1) is None


def test_consumer_rebalance_from_committed_offset():
    raise NotImplementedError


def test_consumer_rebalance_from_uncommitted_offset():
    raise NotImplementedError
