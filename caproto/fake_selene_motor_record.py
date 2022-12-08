#!/usr/bin/env python3
from operator import xor
from textwrap import dedent

from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run
from caproto.server.records import MotorFields


async def broadcast_precision_to_fields(record):
    """Update precision of all fields to that of the given record."""

    precision = record.precision
    for field, prop in record.field_inst.pvdb.items():
        if hasattr(prop, 'precision'):
            await prop.write_metadata(precision=precision)


async def motor_record_simulator(instance,
                                 parent,
                                 async_lib,
                                 defaults=None,
                                 tick_rate_hz=10.):
    """
    A simple motor record simulator.

    Parameters
    ----------
    instance : pvproperty (ChannelDouble)
        Ensure you set ``record='motor'`` in your pvproperty first.

    enabled: pvproperty (ChannelInt)
        Wheter the motor is enabled or not

    async_lib : AsyncLibraryLayer

    defaults : dict, optional
        Defaults for velocity, precision, acceleration, limits, and resolution.

    tick_rate_hz : float, optional
        Update rate in Hz.
    """
    if defaults is None:
        defaults = dict(
            velocity=0.1,
            precision=3,
            acceleration=1.0,
            resolution=1e-6,
            tick_rate_hz=10.,
            user_limits=(0.0, 100.0),
        )

    fields = instance.field_inst  # type: MotorFields
    have_new_position = False

    async def value_write_hook(fields, value):
        nonlocal have_new_position
        # This happens when a user puts to `motor.VAL`
        # print("New position requested!", value)
        have_new_position = True

    fields.value_write_hook = value_write_hook

    await instance.write_metadata(precision=defaults['precision'])
    await broadcast_precision_to_fields(instance)

    await fields.velocity.write(defaults['velocity'])
    await fields.seconds_to_velocity.write(defaults['acceleration'])
    await fields.motor_step_size.write(defaults['resolution'])
    await fields.user_low_limit.write(defaults['user_limits'][0])
    await fields.user_high_limit.write(defaults['user_limits'][1])
    await fields.limit_violation.write(0)

    while True:
        dwell = 1. / tick_rate_hz
        if not parent.can_move:
            await async_lib.library.sleep(dwell)
            continue

        target_pos = instance.value
        diff = (target_pos - fields.user_readback_value.value)
        # compute the total movement time based an velocity
        total_time = abs(diff / fields.velocity.value)
        # compute how many steps, should come up short as there will
        # be a final write of the return value outside of this call
        num_steps = int(total_time // dwell)
        if abs(diff) < 1e-9 and not have_new_position:
            if fields.stop.value != 0:
                await fields.stop.write(0)
            await async_lib.library.sleep(dwell)
            continue

        if fields.stop.value != 0:
            await fields.stop.write(0)

        await fields.done_moving_to_value.write(0)
        await fields.motor_is_moving.write(1)

        readback = fields.user_readback_value.value
        step_size = diff / num_steps if num_steps > 0 else 0.0
        resolution = max((fields.motor_step_size.value, 1e-10))

        for _ in range(num_steps):
            if fields.stop.value != 0:
                await fields.stop.write(0)
                await instance.write(readback)
                break
            if fields.stop_pause_move_go.value == 'Stop':
                await instance.write(readback)
                break

            readback += step_size
            raw_readback = readback / resolution
            await fields.user_readback_value.write(readback)
            await fields.dial_readback_value.write(readback)
            await fields.raw_readback_value.write(raw_readback)
            await async_lib.library.sleep(dwell)
        else:
            # Only executed if we didn't break
            await fields.user_readback_value.write(target_pos)

        await fields.motor_is_moving.write(0)
        await fields.done_moving_to_value.write(1)
        have_new_position = False


class FakeRangeSelector(PVGroup):
    range = pvproperty(name='MCU{index}:FineAdjustment:Select',
                       value='narrow',
                       enum_strings=['wide', 'narrow'],
                       record="bi",
                       dtype=ChannelType.ENUM)
    range_rbv = pvproperty(name='MCU{index}:FineAdjustment:Selected',
                           value='narrow',
                           enum_strings=['wide', 'narrow'],
                           record='bo',
                           dtype=ChannelType.ENUM)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @range.putter
    async def range(self, instance, value):
        # this should check for the pitch positions....
        await self.range_rbv.write(value)


class FakePitchSelector(PVGroup):
    enable = pvproperty(value=0,
                        name='P{index}:Select',
                        dtype=bool,
                        enum_strings=['true', 'false'])
    enable_rbv = pvproperty(value=0,
                            name='P{index}:Selected',
                            dtype=bool,
                            read_only=True,
                            enum_strings=['true', 'false'])
    selectable = pvproperty(value=0,
                            name='P{index}:Selectable',
                            dtype=bool,
                            enum_strings=['true', 'false'])

    def __init__(self, *args, mcu_index, **kwargs):
        super().__init__(*args, **kwargs)
        self.mcu_index = mcu_index

    @selectable.getter
    async def selectable(self, instance):
        if self.enable_rbv.value == 'true':
            return 'true'
        return self.parent.can_enable()[self.mcu_index - 1]

    @enable.putter
    async def enable(self, instance, value):
        if self.selectable.value == 'false':
            return
        if value == 'false':
            return
        current = True if self.enable_rbv.value == "true" else False
        await self.enable_rbv.write(not current)


class FakeMotor(PVGroup):
    motor = pvproperty(value=0.0,
                       name='MCU{index}',
                       record='motor',
                       precision=3)

    def __init__(self,
                 *args,
                 index,
                 velocity=0.1,
                 precision=3,
                 acceleration=1.0,
                 resolution=1e-6,
                 user_limits=(0.0, 100.0),
                 tick_rate_hz=10.,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.index = index
        self._have_new_position = False
        self.tick_rate_hz = tick_rate_hz
        self.defaults = {
            'velocity': velocity,
            'precision': precision,
            'acceleration': acceleration,
            'resolution': resolution,
            'user_limits': user_limits,
        }

    @motor.startup
    async def motor(self, instance, async_lib):
        # Start the simulator:
        await motor_record_simulator(self.motor,
                                     self,
                                     async_lib,
                                     self.defaults,
                                     tick_rate_hz=self.tick_rate_hz)

    @property
    def can_move(self):
        return self.parent.can_move()[self.index - 1]


class FakeSeleneIOC(PVGroup):
    """
    A fake motor IOC, with 3 fake motors.

    PVs
    ---
    mtr1 (motor)
    mtr2 (motor)
    mtr3 (motor)
    """
    range1 = SubGroup(FakeRangeSelector, prefix='SEL2:', macros={'index': 1})
    range2 = SubGroup(FakeRangeSelector, prefix='SEL2:', macros={'index': 2})

    p1 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 1},
                  mcu_index=1)
    p2 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 2},
                  mcu_index=1)
    p3 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 3},
                  mcu_index=1)
    p4 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 4},
                  mcu_index=1)
    p5 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 5},
                  mcu_index=1)
    p6 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 6},
                  mcu_index=1)
    p7 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 7},
                  mcu_index=1)
    p8 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 8},
                  mcu_index=1)
    p9 = SubGroup(FakePitchSelector,
                  prefix='SEL2:',
                  macros={'index': 9},
                  mcu_index=1)
    p10 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 10},
                   mcu_index=1)
    p11 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 11},
                   mcu_index=1)
    p12 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 12},
                   mcu_index=1)
    p13 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 13},
                   mcu_index=1)
    p14 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 14},
                   mcu_index=1)
    p15 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 15},
                   mcu_index=1)
    p16 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 16},
                   mcu_index=1)
    p17 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 17},
                   mcu_index=1)
    p18 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 18},
                   mcu_index=1)
    p19 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 19},
                   mcu_index=2)
    p20 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 20},
                   mcu_index=2)
    p21 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 21},
                   mcu_index=2)
    p22 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 22},
                   mcu_index=2)
    p23 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 23},
                   mcu_index=2)
    p24 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 24},
                   mcu_index=2)
    p25 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 25},
                   mcu_index=2)
    p26 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 26},
                   mcu_index=2)
    p27 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 27},
                   mcu_index=2)
    p28 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 28},
                   mcu_index=2)
    p29 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 29},
                   mcu_index=2)
    p30 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 30},
                   mcu_index=2)
    p31 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 31},
                   mcu_index=2)
    p32 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 32},
                   mcu_index=2)
    p33 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 33},
                   mcu_index=2)
    p34 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 34},
                   mcu_index=2)
    p35 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 35},
                   mcu_index=2)
    p36 = SubGroup(FakePitchSelector,
                   prefix='SEL2:',
                   macros={'index': 36},
                   mcu_index=2)

    mcu1 = SubGroup(FakeMotor,
                    velocity=1.,
                    precision=3,
                    user_limits=(0, 10),
                    index=1,
                    prefix='SEL2:',
                    macros={'index': 1})
    mcu2 = SubGroup(FakeMotor,
                    velocity=1.,
                    precision=3,
                    user_limits=(0, 10),
                    index=2,
                    prefix='SEL2:',
                    macros={'index': 2})

    def can_move(self):
        return (any([
            pitch.enable_rbv.value in ['true', 1]
            for pitch in (self.p1, self.p2, self.p3, self.p4, self.p5, self.p6,
                          self.p7, self.p8, self.p9, self.p10, self.p11,
                          self.p12, self.p13, self.p14, self.p15, self.p16,
                          self.p17, self.p18)
        ]),
                any([
                    pitch.enable_rbv.value in ['true', 1]
                    for pitch in (self.p19, self.p20, self.p21, self.p22,
                                  self.p23, self.p24, self.p25, self.p26,
                                  self.p27, self.p28, self.p29, self.p30,
                                  self.p31, self.p32, self.p33, self.p34,
                                  self.p35, self.p36)
                ]))

    def can_enable(self):
        return (all([
            pitch.enable_rbv.value in ['true', 0]
            for pitch in (self.p1, self.p2, self.p3, self.p4, self.p5, self.p6,
                          self.p7, self.p8, self.p9, self.p10, self.p11,
                          self.p12, self.p13, self.p14, self.p15, self.p16,
                          self.p17, self.p18)
        ]),
                all([
                    pitch.enable_rbv.value in ['true', 0]
                    for pitch in (self.p19, self.p20, self.p21, self.p22,
                                  self.p23, self.p24, self.p25, self.p26,
                                  self.p27, self.p28, self.p29, self.p30,
                                  self.p31, self.p32, self.p33, self.p34,
                                  self.p35, self.p36)
                ]))


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(default_prefix='SQ:AMOR:',
                                              desc=dedent(
                                                  FakeSeleneIOC.__doc__))
    ioc = FakeSeleneIOC(**ioc_options)
    run(ioc.pvdb, **run_options)
