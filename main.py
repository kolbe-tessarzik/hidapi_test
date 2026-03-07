import hid
import pygame
import sys
import time
import threading
import struct
import json
from collections import namedtuple, deque
from typing import TypedDict, NotRequired, Protocol, Any
import math

WIDTH, HEIGHT = 900, 900

# Define the structure of the dictionary
class DevDict(TypedDict):
    serial_number: NotRequired[str]
    product_id: int
    path: bytes

StickCal = namedtuple('StickCal', ['center', 'min', 'max', 'dead'])

def stick_cal_from_dict(d: dict) -> StickCal:
    return StickCal(center=d["center"], min=d["min"], max=d["max"], dead=10)

NINTENDO_VID = 0x057E

try:
    with open("cal.json") as f:
        custom_cal = json.load(f)
except FileNotFoundError:
    custom_cal = {}

# right joycon PID: 0x2007
# left joycon PID: 0x2006
# pro controller PID: 0x2009

def switch_connect_wave():
    frames = []

    # fast attack
    for a in (0x30, 0x50):
        frames.append([
            0xC0,0x10,a,0x30,
            0xC0,0x10,a,0x30
        ])

    # short sustain
    for _ in range(3):
        frames.append([
            0xC0,0x20,0x4F,0x30,
            0xC0,0x20,0x4F,0x30
        ])

    # decay
    for a in (0x40, 0x30, 0x00):
        frames.append([
            0x80,0x40,a,0x30,
            0x80,0x40,a,0x30
        ])

    brake = [
        0x10,0x10,0x40,0x10,
        0x10,0x10,0x40,0x10
    ]


    frames.append(brake)  # brief brake
    frames.append(brake)  # brief brake


    return frames

def normalize(pkt):
    # If first byte is not valid report id, prepend dummy
    if pkt and pkt[0] not in (0x21, 0x30):
        pkt = bytes([0]) + pkt
    return pkt

def int_3_byte(data, little_endian):
    if little_endian:
        data = data[::-1]
    # big endian
    return data[0] << 16 | data[1] << 8 | data[2]

class HIDControllerManager:
    def __init__(self):
        self.controllers: list[GenericHIDController] = []
        self.inactive_controllers: list[GenericHIDController] = []
        self.packet_num: int = 0

    def find_nintendo_devices(self) -> list[DevDict]:
        devs = [d for d in hid.enumerate() if d["vendor_id"] == NINTENDO_VID]
        repeats = []
        for d in devs:
            for controller in (self.controllers + self.inactive_controllers):
                if controller.connected and controller.owns_device(d):
                    # already connected to this device
                    repeats.append(d)
        # delete all repeated devices
        for d in repeats:
            del devs[devs.index(d)]
        return devs


    def open_devices(self):
        devs = self.find_nintendo_devices()
        opened = []
        for d in devs:
            print(d["path"])
            try:
                device = hid.Device( path=d["path"] )
            except hid.HIDException:
                print(f"WARNING: Failed to open device at path: {d["path"]}")
                continue
            for controller in (self.controllers + self.inactive_controllers):
                if not controller.connected and controller.owns_device(d):
                    # same conroller, reconnect
                    controller.reconnect(device, d)
                    break # continue with `for d in devs` loop
            else:
                # if no matching controller found
                cont = get_generic_controller(device=device, info=d)
                print("Create new controller instance")

                opened.append(cont)
        for controller in (self.controllers + self.inactive_controllers):
            controller.update()
        joycon_pressing_l: GenericHIDController | None = None
        joycon_pressing_r: GenericHIDController | None = None

        activated = []
        for i, controller in enumerate(self.inactive_controllers):
            if (controller.l or controller.zl) and (controller.r or controller.zr):
                del self.inactive_controllers[i]
                activated.append(controller)
                controller.play_rumble_async( switch_connect_wave(), frame_delay=0.005)
                print(self.inactive_controllers)
                break
            elif isinstance(controller, GenericHIDLeftJoycon) and (controller.raw_l or controller.raw_zl):
                joycon_pressing_l = controller
            elif isinstance(controller, GenericHIDRightJoycon) and (controller.raw_r or controller.raw_zr):
                joycon_pressing_r = controller

        if joycon_pressing_l and joycon_pressing_r:
            print("Two joycon press")

            assert(isinstance(joycon_pressing_l, GenericHIDLeftJoycon) and isinstance(joycon_pressing_r, GenericHIDRightJoycon))
            controller = GenericHIDTwoJoycons(joycon_pressing_l, joycon_pressing_r)
            activated.append(controller)
            controller.play_rumble_async( switch_connect_wave(), frame_delay=0.005)
            del self.inactive_controllers[self.inactive_controllers.index(joycon_pressing_l)]
            del self.inactive_controllers[self.inactive_controllers.index(joycon_pressing_r)]



        self.inactive_controllers += opened
        self.controllers += activated
        return activated

    def on_exit(self):
        for controller in (self.controllers + self.inactive_controllers):
            controller.play_frame(light=0x01)

class HIDController:
    l_stick_cal_addrs = (0x8010, 0x603D)
    r_stick_cal_addrs = (0x803D, 0x8026, 0x6046, 0x8025)
    stick_cal_for_pid = {
        0x2009: StickCal(center=(2075, 2050), min=(550, 550),  max=(3600, 3550), dead=10),
        0x2007: StickCal(center=None,         min=(770, 725),  max=(3400, 2800), dead=10),
        0x2006: StickCal(center=None,         min=(650, 1200), max=(3130, 3400), dead=10),
    }


    def __init__(self, device: hid.Device, info: DevDict):
        self.device: hid.Device = device
        self.info = info
        self.packet_num: int = 0
        self._recent_data: bytes | None = None
        self.connected = True
        self.serial: str | None = self.info.get('serial_number')
        self.pid:    int | None = self.info.get('product_id'   )
        self.player: int = 0

        self.raw: bool = False

        self.last_keepalive: float = time.time()

        self.r_stick: tuple[int, int] = (0, 0)
        self.l_stick: tuple[int, int] = (0, 0)

        if self.serial in custom_cal.keys():
            self._l_stick_cal = stick_cal_from_dict(custom_cal[self.serial])
            self._r_stick_cal = stick_cal_from_dict(custom_cal[self.serial])
        else:
            self._l_stick_cal = HIDController.stick_cal_for_pid[self.pid]
            self._r_stick_cal = HIDController.stick_cal_for_pid[self.pid]

        self.accel_offset = (0, 0, 0)
        self.accel_scale  = (16384, 16384, 16384)
        self.gyro_offset  = (0, 0, 0)
        self.gyro_scale   = (16, 16, 16)

        self._rumble_req_frames: deque[bytes] = deque()
        self._light_req_frames: deque[int]  = deque()
        self.rumble_frame_rate = 0.04

        self.buttonsDict = {
            "zl": False,
            "zr": False,
            "l": False,
            "r": False,
            "plus": False,
            "minus": False,
            "home": False,
            "capture": False,
            "a": False,
            "b": False,
            "x": False,
            "y": False,
            "up": False,
            "down": False,
            "left": False,
            "right": False,
            "l3": False,
            "r3": False,
            "sl_left": False,
            "sr_left": False,
            "sl_right": False,
            "sr_right": False,
            "ax": False,
            "ay": False,
            "az": False,
            "gx": False,
            "gy": False,
            "gz": False,
        }

        self.prepare_controller()
        print("Starting thread . . .")
        self.rumble_thread = threading.Thread(target=self._rumble_worker, daemon=True)
        self.rumble_thread.start()
        self.set_light_animation([0xFF]*5 + [0x00]*10)

    def prepare_controller(self):
        # Switch to full 0x30 input reports
        self.send_subcommand(0x03, payload=bytes([0x30]))
        time.sleep(0.1)
        # get stick calibration
        self.read_stick_cals()
        # Enable tilt controls
        self.send_subcommand(0x40, payload=bytes([0x01]))
        # enable rumble
        self.send_subcommand(0x48, payload=bytes([0x01]))
        # set player lights
        self.send_subcommand(0x30, payload=bytes([0x03]))

    def reconnect(self, device: hid.Device, info: DevDict):
        self.device = device
        self.info = info
        self.connected = True
        # self.serial = self.info.get('serial_number')
        # self.pid = self.info.get('product_id')

    def owns_device(self, info: DevDict) -> bool:
        path = self.info.get("path")
        if path is not None and path == info.get("path"):
            return True
        return self.serial is not None and self.serial == info.get("serial_number")

    def __getattr__(self, name: str):
        if name == "l_stick":
            return self.l_stick
        elif name == "r_stick":
            return self.r_stick
        elif name.startswith("raw_"):
            return self.buttonsDict[name[4:]]
        return self.buttonsDict[name]

    def write_to_device(self, data):
        try:
            self.device.write(bytes(data))
        except hid.HIDException as e:
            print(f"WARNING: device was disconnected.")
            self.connected = False

    def send_subcommand(self, subcmd, rumble=b"", payload=b""):
        # 49-byte output report (Bluetooth)
        report = bytearray(49)

        report[0] = 0x01                     # Output report ID
        report[1] = self.packet_num & 0x0F   # Packet counter (0–15)

        for i, rumble_byte in enumerate(rumble):
            report[2+i] = rumble_byte

        # Bytes 2..9 = rumble data (leave zero)
        report[10] = subcmd
        report[11:11+len(payload)] = payload

        self.packet_num += 1
        self.write_to_device(report)

    def play_rumble_async(self, frames, frame_delay=0.015):
        self._rumble_req_frames.extend(frames)
        self.rumble_frame_rate = frame_delay

    def set_light_animation(self, frames: list[int]):
        self._light_req_frames.extend(frames)

    def set_player(self, num: int):
        if num > 4 or num < 0:
            raise(ValueError(f"invalid player number {num}"))
        self.player = num

    def get_player_lights(self, num):
        match num:
            case 1:
                return 0b0001
            case 2:
                return 0b0011
            case 3:
                return 0b0111
            case 4:
                return 0b1111
            case _:
                return num

    def _rumble_worker(self):
        next_tick = time.monotonic()

        while True:
            did_something = False

            # ---- rumble ----
            if self._rumble_req_frames:
                did_something = True
                rumble = self._rumble_req_frames.popleft()
            else:
                rumble = b""

            # ---- lights ----
            if self._light_req_frames:
                did_something = True
                lights = self._light_req_frames.popleft()
            else:
                lights = self.get_player_lights(self.player)

            if did_something:
                self.play_frame(rumble, lights)

            # ---- frame pacing ----
            next_tick += self.rumble_frame_rate
            sleep = next_tick - time.monotonic()

            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()

    def play_frame(self, rumble=b"", light=0x00):
        """
        Send a combination of rumble data and light data to the controller.
        Defaults to no rumble, lights off
        """
        self.send_subcommand(0x30, rumble=rumble, payload=bytes([light]))


    def play_rumble_frame(self, frame):
        report = bytearray(49)
        report[0] = 0x01
        report[1] = self.packet_num & 0x0F
        report[2:10] = bytes(frame)
        report[10] = 0x00
        self.packet_num += 1
        self.write_to_device(report)

    def play_rumble(self, frames, frame_delay=0.015):
        """
        frames: list of 8-byte lists
        frame_delay: seconds per frame (~60Hz)
        """

        for frame in frames:
            self.play_rumble_frame(frame)
            time.sleep(frame_delay)

        # stop vibration
        self.send_subcommand(0x00)

    def stop_rumble(self):
        self.send_subcommand(0x00)

    def read_spi(self, addr, size):
        self.send_subcommand(0x10, payload=bytes([
            addr & 0xFF,
            (addr >> 8) & 0xFF,
            (addr >> 16) & 0xFF,
            size
        ]))

    def read_stick_cals(self):
        match self.info['product_id']:
            case 0x2009: # pro controller
                # left stick
                self.read_spi(0x8010, 18)
                # right stick
                self.read_spi(0x6046, 18)
            case 0x2006:
                # left joycon
                self.read_spi(0x8010, 18)
            case 0x2007:
                # right joycon
                self.read_spi(0x8026, 18)

    def read_tilt_control_cal(self, data):
        sys_cal = struct.unpack("<12h", data[:24])

        self.accel_offset = sys_cal[0:3]
        self.accel_scale  = sys_cal[3:6]
        self.gyro_offset  = sys_cal[6:9]
        self.gyro_scale   = sys_cal[9:12]

    def parse_stick_cal(self, data):

        cx, cy, minx, miny, maxx, maxy, dead, _ = struct.unpack("<HHHHHHHH", data[:16])
        print("Recieved stick calibration")
        print(cx, cy, minx, miny, maxx, maxy, dead)

        return StickCal(
            center=(cx, cy),
            min=(minx, miny),
            max=(maxx, maxy),
            dead=dead
        )

    def read_spi_response(self, data):
        addr = int_3_byte(data[0:3], True)
        size = data[3]

        payload = data[4:4+size]

        if addr == 0x6080:
            self.read_tilt_control_cal(payload[:24])
        elif addr in HIDController.l_stick_cal_addrs:
            # self._l_stick_cal = self.parse_stick_cal(payload)
            pass
        elif addr in HIDController.r_stick_cal_addrs:
            # self._r_stick_cal = self.parse_stick_cal(payload)
            pass
        else:
            print(f"unrecognized memory addr: {hex(addr)}")
            return

    def unpack_command_response(self, data):
        assert(len(data) >= 13)
        # for b in data:
        #     print(hex(b), end=" ")
        # print("\n")
        cmd = data[14]
        match int(cmd):
            case 0x10:
                print("Read spi response")
                self.read_spi_response(data[15:])
            case _:
                return


    def unpack_tilt_controls(self):
        assert(self._recent_data is not None)
        # First IMU sample starts at byte 13
        base = 13

        self.ax, self.ay, self.az, self.gx, self.gy, self.gz = struct.unpack_from("<hhhhhh", self._recent_data, base)

    def scale_axis(self, value: int, axis: int, cal: StickCal) -> int:
        """
        Convert raw 12-bit stick value to -100 .. 100 range
        """
        min_val = cal.min[axis]
        max_val = cal.max[axis]
        try:
            center = cal.center[axis]
        except TypeError:
             # if center is not given, calculate it
             center = (min_val + max_val) / 2
        # Shift relative to center
        delta = value - center

        # Scale separately for negative vs positive
        if delta < 0:
            scaled = delta / (center - min_val) * 100
        else:
            scaled = delta / (max_val - center) * 100

        # apply dead zone
        if abs(scaled) < cal.dead:
            scaled = 0


        # scale extra so it's not as hard to get to 100
        scaled *= 1.02

        # Clamp
        if scaled < -100: scaled = -100
        if scaled > 100: scaled = 100

        return int(scaled)

    def left_stick(self, report):
        """
        report: list or bytes from 0x30 input
        returns: (x, y) tuple scaled -100..100
        """
        # raw 12-bit values
        raw_x = report[6] | ((report[7] & 0x0F) << 8)
        raw_y = ((report[7] >> 4) | (report[8] << 4))

        x =  self.scale_axis(raw_x, 0, self._l_stick_cal)
        y = -self.scale_axis(raw_y, 1, self._l_stick_cal)
        if self.raw:
            x = raw_x
            y = raw_y

        return (x, y)


    def right_stick(self, report):
        """
        report: list or bytes from 0x30 input
        returns: (x, y) tuple scaled -100..100
        """
        raw_x = report[9] | ((report[10] & 0x0F) << 8)
        raw_y = ((report[10] >> 4) | (report[11] << 4))

        x =  self.scale_axis(raw_x, 0, self._r_stick_cal)
        y = -self.scale_axis(raw_y, 1, self._r_stick_cal)

        if self.raw:
            x = raw_x
            y = raw_y

        return (x, y)

    def set_raw(self, raw: bool):
        self.raw = raw

    def read_data_raw(self):
        # read all available data, return the latest complete packet
        # this avoids lag from buffered packets
        latest = None

        while True:
            try:
                d = self.device.read(64, timeout=0)
            except hid.HIDException as e:
                if str(e) == "Success":
                    break
                raise(e)

            if not d:
                break

            if len(d) >= 13 and d[0] == 0x21:
                self.unpack_command_response(d)

            latest = d

        # ignore 0x21 sucommand replies
        if latest and  latest[0] == 0x30:
            return latest
        else:
            return self._recent_data

    def get_button_state(self, bit_index):
        assert(self._recent_data is not None)
        buttons_bytes = (self._recent_data[3] << 16) | (self._recent_data[4] << 8) | (self._recent_data[5])
        return bool(buttons_bytes & (1 << bit_index))

    def update(self):
        try:
            self._recent_data = self.read_data_raw()
        except hid.HIDException as e:
            # print(e)
            # print("Device timed out")
            self.connected = False
            return
        # if self._recent_data:
        #     print(f"Report ID: 0x{self._recent_data[0]:02x}")

        if (time.time() - self.last_keepalive) > 1.0:
            # Switch to full 0x30 input reports
            # unecessary, but to keep controller on
            self.send_subcommand(0x03, payload=bytes([0x30]))
            self.last_keepalive = time.time()

        # 10/11 = r stick X/Y
        if self._recent_data:
            self.r_stick = self.right_stick(self._recent_data)
            self.l_stick = self.left_stick(self._recent_data)

            self.buttonsDict["zl"]       = self.get_button_state(7 )
            self.buttonsDict["zr"]       = self.get_button_state(23)
            self.buttonsDict["l"]        = self.get_button_state(6 )
            self.buttonsDict["r"]        = self.get_button_state(22)
            self.buttonsDict["plus"]     = self.get_button_state(9 )
            self.buttonsDict["minus"]    = self.get_button_state(8 )
            self.buttonsDict["home"]     = self.get_button_state(12)
            self.buttonsDict["capture"]  = self.get_button_state(13)
            self.buttonsDict["a"]        = self.get_button_state(19)
            self.buttonsDict["b"]        = self.get_button_state(18)
            self.buttonsDict["x"]        = self.get_button_state(17)
            self.buttonsDict["y"]        = self.get_button_state(16)
            self.buttonsDict["up"]       = self.get_button_state(1 )
            self.buttonsDict["down"]     = self.get_button_state(0 )
            self.buttonsDict["left"]     = self.get_button_state(3 )
            self.buttonsDict["right"]    = self.get_button_state(2 )
            self.buttonsDict["l3"]       = self.get_button_state(11)
            self.buttonsDict["r3"]       = self.get_button_state(10)
            self.buttonsDict["sl_left"]  = self.get_button_state(5 )
            self.buttonsDict["sr_left"]  = self.get_button_state(4 )
            self.buttonsDict["sl_right"] = self.get_button_state(21)
            self.buttonsDict["sr_right"] = self.get_button_state(20)

            self.unpack_tilt_controls()

class GenericHIDController(Protocol):
    zl: bool
    zr: bool
    l:  bool
    r:  bool

    a: bool
    b: bool
    x: bool
    y: bool

    plus:    bool
    minus:   bool
    home:    bool
    capture: bool

    click: bool

    stick: tuple[int, int]

    _recent_data: bytes

    device: hid.Device
    info:   DevDict

    connected: bool

    serial: str | None
    pid: int | None

    def update(self) -> None: ...

    def set_player(self, num: int) -> None: ...

    def reconnect(self, dev: hid.Device, info: DevDict) -> None: ...

    def play_frame(self, rumble=None, light=None) -> None: ...

    def owns_device(self, info: DevDict) -> bool: ...

    # HIDController exposes dynamic names such as raw_* via __getattr__.
    # Returning Any lets callers use those names without listing them here.
    def __getattr__(self, name: str) -> Any: ...

# BC:CE:25:6E:EF:D8
class GenericHIDControllerImpl(HIDController, GenericHIDController):
    def __init__(self, device, info):
        # ensure self.button_mapping exists
        try:
            self.button_mapping
        except:
            self.button_mapping = {}
        # self.stick = (0, 0)
        super().__init__(device, info)

    def __getattr__(self, name):
        if name in self.button_mapping.keys():
            return super().__getattr__(self.button_mapping[name])
        else:
            return super().__getattr__(name)

    @property
    def stick(self) -> tuple[int, int]:
        return self.l_stick

class GenericHIDProController(GenericHIDControllerImpl):
    def __init__(self, device, info):
        self.button_mapping = {"stick": "l_stick", "click": "l3"} # all other mappings are pass-through
        super().__init__(device, info)

class GenericHIDLeftJoycon(GenericHIDControllerImpl):
    def __init__(self, device, info):
        self.button_mapping = {
            "click": "l3",
            "a": "down",
            "b": "left",
            "x": "right",
            "y": "up",
            "l": "sl_left",
            "r": "sr_left",
            "z": "zl",
        }
        super().__init__(device, info)

    @property
    def stick(self) -> tuple[int, int]:
        if self.raw:
            return self.l_stick
        return (self.l_stick[1], -self.l_stick[0])

class GenericHIDRightJoycon(GenericHIDControllerImpl):
    def __init__(self, device, info):
        self.button_mapping = {
            "click": "r3",
            "a": "x",
            "b": "a",
            "x": "y",
            "y": "b",
            "l": "sl_right",
            "r": "sr_right",
            "z": "zr",
        }
        super().__init__(device, info)

    @property
    def stick(self) -> tuple[int, int]:
        if self.raw:
            return self.r_stick
        return (-self.r_stick[1], self.r_stick[0])

def get_generic_controller(device, info) -> GenericHIDController:
    match info['product_id']:
        case 0x2009:
            return GenericHIDProController(device, info)
        case 0x2006:
            return GenericHIDLeftJoycon(device, info)
        case 0x2007:
            return GenericHIDRightJoycon(device, info)
        case _:
            raise(ValueError(f"Unexpected PID: {info['product_id']}"))

class GenericHIDTwoJoycons(GenericHIDController):
    def __init__(self, l_cont: GenericHIDLeftJoycon, r_cont: GenericHIDRightJoycon):
        self.l_cont = l_cont
        self.r_cont = r_cont
        self._recent_data = self.l_cont._recent_data or self.r_cont._recent_data or b""
        self.info = {
            'product_id': 0x2008,
            'path': b'n/a',
        }
        serials = [serial for serial in (self.l_cont.serial, self.r_cont.serial) if serial]
        self.serial = "+".join(serials) if serials else None
        self.pid = 0x2008

    def __getattr__(self, name):
        if name == "stick" or name == "l_stick":
            return self.l_cont.l_stick
        if name == "r_stick":
            return self.r_cont.r_stick
        candidate_names = [name]
        if not name.startswith("raw_"):
            candidate_names.insert(0, f"raw_{name}")

        for candidate_name in candidate_names:
            left_missing = right_missing = False
            try:
                left_value = getattr(self.l_cont, candidate_name)
            except (AttributeError, KeyError):
                left_missing = True
                left_value = None
            try:
                right_value = getattr(self.r_cont, candidate_name)
            except (AttributeError, KeyError):
                right_missing = True
                right_value = None

            if left_missing and right_missing:
                continue
            if left_missing:
                return right_value
            if right_missing:
                return left_value
            if isinstance(left_value, bool) and isinstance(right_value, bool):
                return left_value or right_value
            return left_value

        raise(AttributeError(name))

    def update(self):
        self.l_cont.update()
        self.r_cont.update()
        self._recent_data = self.l_cont._recent_data or self.r_cont._recent_data or b""

    def set_player(self, num):
        self.l_cont.set_player(num)
        self.r_cont.set_player(num)

    def reconnect(self, dev: hid.Device, info: DevDict) -> None:
        if self.l_cont.owns_device(info):
            self.l_cont.reconnect(dev, info)
            return
        if self.r_cont.owns_device(info):
            self.r_cont.reconnect(dev, info)
            return
        raise(ValueError(f"Device does not belong to this Joy-Con pair: {info.get('path')}"))

    def owns_device(self, info: DevDict) -> bool:
        return self.l_cont.owns_device(info) or self.r_cont.owns_device(info)

    def play_frame(self, rumble=b"", light=0x00) -> None:
        self.l_cont.play_frame(rumble=rumble, light=light)
        self.r_cont.play_frame(rumble=rumble, light=light)

    def play_rumble(self, *args, **kwargs):
        self.l_cont.play_rumble(*args, **kwargs)
        self.r_cont.play_rumble(*args, **kwargs)

    def play_rumble_async(self, *args, **kwargs):
        self.l_cont.play_rumble_async(*args, **kwargs)
        self.r_cont.play_rumble_async(*args, **kwargs)


    @property
    def connected(self):
        return self.l_cont.connected and self.r_cont.connected

    @property
    def device(self):
        return self.l_cont.device

    zl: bool
    zr: bool
    l: bool
    r: bool
    a: bool
    b: bool
    x: bool
    y: bool
    plus: bool
    minus: bool
    home: bool
    capture: bool
    click: bool
    stick: tuple[int, int]
    info: DevDict
    serial: str | None
    pid: int | None


def draw_stick(surface, x, y, stick_pos):
    pygame.draw.circle(surface, (0, 0, 255), (x + stick_pos[0], y + stick_pos[1]), 50)

def main():
    # pygame.init()
    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Nintendo Switch Bluetooth HID Demo")

    font = pygame.font.SysFont("monospace", 24)

    clock = pygame.time.Clock()

    manager = HIDControllerManager()
    manager.open_devices()

    cont: GenericHIDController | None = None
    info = None

    try:
        pygame.event.get()
    except SystemError:
        print("First pygame.event.get() failed")

    last_packet = None

    running = True
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False

        screen.fill((20, 20, 20))

        y = 20

        def draw(text):
            nonlocal y
            surf = font.render(text, True, (220, 220, 220))
            screen.blit(surf, (20, y))
            y += 26

        manager.open_devices()
        if not cont:
            if manager.controllers:
                cont = manager.controllers[0]
                info = manager.controllers[0].info
        if cont and info:

            cont.update()
            cont.set_player(1)

            draw("Nintendo Switch Bluetooth HID Demo")
            draw("")

            draw(f"PID=0x{info['product_id']:04x}  {info.get('product_string')}")
            draw(f"     Path: {info['path']}")
            draw("")

            draw(f"Manufacturer: {info.get('manufacturer_string')}")
            draw(f"Product: {info.get('product_string')}")
            draw(f"Serial: {info.get('serial_number')}")
            draw("")

            last_packet = cont._recent_data or last_packet

            draw("Raw (hex):")
            if last_packet:
                hexline = " ".join(f"{b:02x}" for b in last_packet[:32])
            else:
                hexline = "(no packet)"
            draw(hexline)
            draw(f"L: {cont.l}")
            draw(f"R: {cont.r}")
            draw(f"PLUS: {cont.plus}")
            draw(f"MINUS: {cont.minus}")
            draw(f"HOME: {cont.home}")
            draw(f"CAPTURE: {cont.capture}")
            draw(f"A: {cont.a}")
            draw(f"B: {cont.b}")
            draw(f"X: {cont.x}")
            draw(f"Y: {cont.y}")
            draw(f"Click: {cont.click}")
            draw(f"Stick X: {cont.stick[0]}")
            draw(f"Stick Y: {cont.stick[1]}")
            # draw(f"AX: {cont.ax}")
            # draw(f"AY: {cont.ay}")
            # draw(f"AZ: {cont.az}")
            # draw(f"GX: {cont.gx}")
            # draw(f"GY: {cont.gy}")
            # draw(f"GZ: {cont.gz}")

            draw_stick(screen, 300, 500, (cont.stick[0], cont.stick[1]))
            # draw_stick(screen, 600, 700, (cont.r_stick[0], cont.r_stick[1]))


        pygame.display.flip()
        clock.tick(240)

    if cont and cont.device:
        cont.device.close()
    manager.on_exit()
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
