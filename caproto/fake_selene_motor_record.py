#!/usr/bin/env python3
from textwrap import dedent
from operator import xor

from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run
from caproto.server.records import MotorFields


async def broadcast_precision_to_fields(record):
    """Update precision of all fields to that of the given record."""

    precision = record.precision
    for field, prop in record.field_inst.pvdb.items():
        if hasattr(prop, 'precision'):
            await prop.write_metadata(precision=precision)


async def motor_record_simulator(instance, parent, async_lib, defaults=None,
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
    enable = pvproperty(value=0, name='P{index}:Select', dtype=bool)
    enable_rbv = pvproperty(value=0, name='P{index}:Selected', dtype=bool, read_only=True)
    selectable = pvproperty(value=0, name='P{index}:Selectable', dtype=bool)

    @enable.putter
    async def enable(self, instance, value):
        if self.selectable.value == 'Off':
            return
        if value == 'Off':
            return
        current = True if self.enable_rbv.value == "On" else False
        await self.enable_rbv.write(not current)


class FakeMotor(PVGroup):
    motor = pvproperty(value=0.0, name='MCU{index}', record='motor', precision=3)

    def __init__(self, *args,
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
        await motor_record_simulator(
            self.motor, self, 
            async_lib, self.defaults,
            tick_rate_hz=self.tick_rate_hz
        )

    @property
    def can_move(self):
        return self.parent.can_move()


class FakeMotorIOC(PVGroup):
    """
    A fake motor IOC, with 3 fake motors.

    PVs
    ---
    mtr1 (motor)
    mtr2 (motor)
    mtr3 (motor)
    """
    range1 = SubGroup(FakeRangeSelector, prefix='SEL2:', macros={'index':1})
    range2 = SubGroup(FakeRangeSelector, prefix='SEL2:', macros={'index':2})

    motor_opts = dict(velocity=1., precision=3, user_limits=(0, 10))

    p1 = SubGroup(FakePitchSelector, prefix='SEL2:', macros={'index':1})
    p2 = SubGroup(FakePitchSelector, prefix='SEL2:', macros={'index':2})
    p3 = SubGroup(FakePitchSelector, prefix='SEL2:', macros={'index':3})
    p4 = SubGroup(FakePitchSelector, prefix='SEL2:', macros={'index':4})

    mcu1 = SubGroup(FakeMotor, **motor_opts, index=1, prefix='SEL2:', macros={'index':1})
    # mcu2 = SubGroup(FakeMotor, **motor_opts, index=2, prefix='SEL2:', macros={'index':2})

    def can_move(self):
        return any([1 if pitch.enable_rbv.value in ['On', 1] else 0  for pitch in (self.p1,self.p2,self.p3,self.p4)])


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='SQ:AMOR:',
        desc=dedent(FakeMotorIOC.__doc__))
    ioc = FakeMotorIOC(**ioc_options)
    run(ioc.pvdb, **run_options)