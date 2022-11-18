#!/usr/bin/env python3
from textwrap import dedent

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


class pmacV3IOC(PVGroup):
    motor1 = pvproperty(value=1.0, record='motor')
    # enable = pvproperty(value=0, name='motor1:Enable', dtype=int)
    # enable_rbv = pvproperty(value=0, name='motor1:Enable_RBV', dtype=int, read_only=True)
    #
    # @enable.putter
    # async def enable(self, instance, value):
    #     await self.enable_rbv.write(value)


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='simple:',
        desc='Run an IOC that mocks a pmacV3 controller'
    )
    ioc = pmacV3IOC(**ioc_options)
    run(ioc.pvdb, **run_options)