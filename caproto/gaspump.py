#!/usr/bin/env python3

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run, SubGroup

import asyncio

HOST = '127.0.0.1'
PORT = 3001


async def connect(instance):
    while True:
        try:
            instance.reader, instance.writer = await asyncio.open_connection(instance.host, instance.port)
        except ConnectionRefusedError:
            await asyncio.sleep(5)
            continue


class GasPumpController:

    def __init__(self, host=HOST, port=PORT):
        self.db = {}
        self.reader = None
        self.writer = None
        self.host = host
        self.port = port

    async def send(self, message):
        try:
            self.writer.write(message.encode())
            await self.writer.drain()
            data = await self.reader.read(1024)

        except (ConnectionResetError, AttributeError):
            await connect(self)
        print(f'Received: {data.decode()!r}')


class gaspumpIOC(PVGroup):
    amplitude_rbv = pvproperty(value=10000, name='Amplitude_RBV', dtype=int, read_only=True)
    amplitude = pvproperty(value=0, name='Amplitude', dtype=int)
    frequency_rbv = pvproperty(value=50, name='Frequency_RBV', dtype=int, read_only=True)
    frequency = pvproperty(value=0, name='Frequency', dtype=int)
    phase_rbv = pvproperty(value=0, name='Phase_RBV', dtype=int, read_only=True)
    phase = pvproperty(value=0, name='Phase', dtype=int)
    mode_rbv = pvproperty(value=1, name='Mode_RBV', dtype=int, read_only=True)
    error = pvproperty(value='', name='Error', dtype=str)

    controller = GasPumpController()


    # @phase.putter
    # async def phase(self, instance, value):
    #     channel, name = instance.name.split(".")[-2:]
    #     await self.controller.send(f'{instance} {value}')

    @amplitude.putter
    async def amplitude(self, instance, value):
        await self.amplitude_rbv.write(value)

    @frequency.putter
    async def frequency(self, instance, value):
        await self.frequency_rbv.write(value)


class GaspumpSim(PVGroup):
    ch1 = SubGroup(gaspumpIOC, prefix='Ch1:')
    ch2 = SubGroup(gaspumpIOC, prefix='Ch2:')


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='SQ:DMC:gaspump:',
        desc='Run an IOC that simulate the DMC gaspump'
    )
    ioc = GaspumpSim(**ioc_options)
    run(ioc.pvdb, **run_options)