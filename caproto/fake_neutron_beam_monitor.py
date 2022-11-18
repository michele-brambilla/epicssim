#!/usr/bin/env python3
import time
import uuid
from textwrap import dedent
from typing import Tuple

from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run
from confluent_kafka import Consumer


def get_broker_and_topic_from_uri(uri: str) -> Tuple[str, str]:
    if "/" not in uri:
        raise RuntimeError(
            f"Unable to parse URI {uri}, should be of form localhost:9092/topic"
        )
    topic = uri.split("/")[-1]
    broker = "".join(uri.split("/")[:-1])
    broker = broker.strip("/")
    return broker, topic


def create_consumer(broker_address: str) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": broker_address,
            "group.id": uuid.uuid4(),
            "default.topic.config": {"auto.offset.reset": "latest"},
        }
    )


async def beam_monitor_simulator(instance, async_lib, consumer):
    """
    """
    dwell = 0.001
    while True:
        message = consumer.consume(1, timeout=1)
        print(f'message: {message} @ {time.time()}')
        await async_lib.library.sleep(dwell)


class IocBeamMonitor(PVGroup):
    '''
    Hello
    '''
    rate = pvproperty(value=0, doc='Neutron rate on monitor')

    def __init__(self, *args,
                 kafka_uri='ess01/amor_detector',
                 **kwargs):
        super().__init__(*args, **kwargs)
        broker, topic = get_broker_and_topic_from_uri(kafka_uri)
        consumer = create_consumer(broker_address=broker)
        consumer.subscribe([topic])
        self.consumer = consumer

    @rate.startup
    async def rate(self, instance, async_lib):
        # Start the simulator:
        await beam_monitor_simulator(
            self.rate, async_lib, self.consumer
        )


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='sim:',
        desc=dedent(IocBeamMonitor.__doc__))
    ioc = IocBeamMonitor(**ioc_options)
    run(ioc.pvdb, **run_options)
