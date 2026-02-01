import hid
import pygame
import sys
import time
import threading
import struct
from collections import namedtuple
import math

WIDTH, HEIGHT = 900, 900

pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Nintendo Switch Bluetooth HID Demo")

font = pygame.font.SysFont("monospace", 24)

StickCal = namedtuple('StickCal', ['center', 'min', 'max', 'dead'])

NINTENDO_VID = 0x057E


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
        self.controllers = []
        self.inactive_controllers = []
        self.packet_num = 0

    def find_nintendo_devices(self):
        devs = [d for d in hid.enumerate() if d["vendor_id"] == NINTENDO_VID]
        repeats = []
        for d in devs:
            for controller in (self.controllers + self.inactive_controllers):
                if controller.connected and controller.serial == d.get('serial_number'):
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
                if not controller.connected and controller.serial == d.get('serial_number'):
                    # same conroller, reconnect
                    controller.reconnect(device)
                    break # continue with `for d in devs` loop
            else:
                # if no matching controller found
                cont = HIDController(device=device, info=d)

                opened.append(cont)
        activated = []
        for controller in self.inactive_controllers:
            controller.update()
            if (controller.l or controller.zl) and (controller.r or controller.zr):
                del self.inactive_controllers[self.inactive_controllers.index(controller)]
                activated.append(controller)
                controller.play_rumble_async( switch_connect_wave(), frame_delay=0.005)

        self.inactive_controllers += opened
        self.controllers += activated
        return activated

class HIDController:
    l_stick_cal_addrs = (0x8010, 0x603D)
    r_stick_cal_addrs = (0x803D, 0x8026, 0x6046, 0x8025)

    def __init__(self, device, info):
        self.device = device
        self.info = info
        self.packet_num = 0
        self._recent_data = None
        self.connected = True
        self.serial = self.info.get('serial_number')

        self.last_keepalive = time.time()

        self.r_stick = (0, 0)
        self.l_stick = (0, 0)

        self._l_stick_cal = StickCal(center=(2075, 2050), min=(550, 550), max=(3600, 3550), dead=10)
        self._r_stick_cal = StickCal(center=(2075, 2050), min=(550, 550), max=(3600, 3550), dead=10)

        self.accel_offset = (0, 0, 0)
        self.accel_scale  = (16384, 16384, 16384)
        self.gyro_offset  = (0, 0, 0)
        self.gyro_scale   = (16, 16, 16)

        self._req_frames = []
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
            "ax": False,
            "ay": False,
            "az": False,
            "gx": False,
            "gy": False,
            "gz": False,
        }

        self.prepare_controller()
        self.rumble_thread = None

    def prepare_controller(self):
        # Switch to full 0x30 input reports
        self.send_subcommand(0x03, bytes([0x30]))
        time.sleep(0.1)
        # get stick calibration
        self.read_stick_cals()
        # Enable tilt controls
        self.send_subcommand(0x40, bytes([0x01]))
        # enable rumble
        self.send_subcommand(0x48, bytes([0x01]))

    def reconnect(self, device):
        self.device = device
        self.connected = True

    def __getattr__(self, name):
        if name in self.buttonsDict.keys():
            return self.buttonsDict[name]
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def send_subcommand(self, subcmd, payload=b""):
        # 49-byte output report (Bluetooth)
        report = bytearray(49)

        report[0] = 0x01                     # Output report ID
        report[1] = self.packet_num & 0x0F   # Packet counter (0–15)

        # Bytes 2..9 = rumble data (leave zero)
        report[10] = subcmd
        report[11:11+len(payload)] = payload

        self.packet_num += 1
        try:
            self.device.write(bytes(report))
        except hid.HIDException:
            print("WARNING: device was disconnected")
            self.connected = False

    def play_rumble_async(self, frames, frame_delay=0.015):
        if not self.rumble_thread:
            print("Starting thread . . .")
            self.rumble_thread = threading.Thread(target=self._rumble_worker, daemon=True)
            self.rumble_thread.start()
        self._req_frames = frames
        self.rumble_frame_rate = frame_delay

    def _rumble_worker(self):
        while True:
            if self._req_frames:
                self.play_rumble_frame(self._req_frames[0])
                del self._req_frames[0]
                time.sleep(self.rumble_frame_rate)
            else:
                self.stop_rumble()
                time.sleep(0.04)

    def play_rumble_frame(self, frame):
        report = bytearray(49)
        report[0] = 0x01
        report[1] = self.packet_num & 0x0F
        report[2:10] = bytes(frame)
        report[10] = 0x00
        self.packet_num += 1
        self.device.write(bytes(report))


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
        self.send_subcommand(0x10, bytes([
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
                print(f"unrecognized cmd: {int(cmd)}")
                return


    def unpack_tilt_controls(self):
        assert(self._recent_data is not None)
        # First IMU sample starts at byte 13
        base = 13

        self.ax, self.ay, self.az, self.gx, self.gy, self.gz = struct.unpack_from("<hhhhhh", self._recent_data, base)

    def scale_axis(self, value, axis, cal):
        """
        Convert raw 12-bit stick value to -100 .. 100 range
        """
        center = cal.center[axis]
        min_val = cal.min[axis]
        max_val = cal.max[axis]
        # Shift relative to center
        delta = value - center

        # Scale separately for negative vs positive
        if delta < 0:
            scaled = delta / (center - min_val) * 100
        else:
            scaled = delta / (max_val - center) * 100

        # Clamp
        if scaled < -100: scaled = -100
        if scaled > 100: scaled = 100

        #apply dead zone
        if abs(scaled) < cal.dead:
            scaled = 0
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

        return (x, y)

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

    def get_button_state(self, bit_index):
        assert(self._recent_data is not None)
        buttons_bytes = (self._recent_data[3] << 16) | (self._recent_data[4] << 8) | (self._recent_data[5])
        return bool(buttons_bytes & (1 << bit_index))

    def update(self):
        try:
            self._recent_data = self.read_data_raw()
        except hid.HIDException as e:
            print("Device timed out")
            self.connected = False
            return
        # if self._recent_data:
        #     print(f"Report ID: 0x{self._recent_data[0]:02x}")

        if (time.time() - self.last_keepalive) > 1.0:
            # Switch to full 0x30 input reports
            # unecessary, but to keep controller on
            self.send_subcommand(0x03, bytes([0x30]))
            self.last_keepalive = time.time()

        # 10/11 = r stick X/Y
        if self._recent_data:
            self.r_stick = self.right_stick(self._recent_data)
            self.l_stick = self.left_stick(self._recent_data)

            self.buttonsDict["zl"]      = self.get_button_state(7)
            self.buttonsDict["zr"]      = self.get_button_state(23)
            self.buttonsDict["l"]       = self.get_button_state(6)
            self.buttonsDict["r"]       = self.get_button_state(22)
            self.buttonsDict["plus"]    = self.get_button_state(9)
            self.buttonsDict["minus"]   = self.get_button_state(8)
            self.buttonsDict["home"]    = self.get_button_state(12)
            self.buttonsDict["capture"] = self.get_button_state(13)
            self.buttonsDict["a"]       = self.get_button_state(19)
            self.buttonsDict["b"]       = self.get_button_state(18)
            self.buttonsDict["x"]       = self.get_button_state(17)
            self.buttonsDict["y"]       = self.get_button_state(16)
            self.buttonsDict["up"]      = self.get_button_state(1)
            self.buttonsDict["down"]    = self.get_button_state(0)
            self.buttonsDict["left"]    = self.get_button_state(3)
            self.buttonsDict["right"]   = self.get_button_state(2)
            self.buttonsDict["l3"]     = self.get_button_state(11)
            self.buttonsDict["r3"]     = self.get_button_state(10)

            self.unpack_tilt_controls()

def draw_stick(surface, x, y, stick_pos):
    pygame.draw.circle(surface, (0, 0, 255), (x + stick_pos[0], y + stick_pos[1]), 50)

def main():
    clock = pygame.time.Clock()

    manager = HIDControllerManager()
    manager.open_devices()

    cont = None
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

        if not cont:
            manager.open_devices()
            if manager.controllers:
                cont = manager.controllers[0]
                info = manager.controllers[0].info
        if cont:

            cont.update()

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
            draw(f"ZL: {cont.zl}")
            draw(f"ZR: {cont.zr}")
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
            draw(f"UP: {cont.up}")
            draw(f"DOWN: {cont.down}")
            draw(f"LEFT: {cont.left}")
            draw(f"RIGHT: {cont.right}")
            draw(f"L3: {cont.l3}")
            draw(f"R3: {cont.r3}")
            draw(f"R Stick X: {cont.r_stick[0]}")
            draw(f"R Stick Y: {cont.r_stick[1]}")
            # draw(f"AX: {cont.ax}")
            # draw(f"AY: {cont.ay}")
            # draw(f"AZ: {cont.az}")
            # draw(f"GX: {cont.gx}")
            # draw(f"GY: {cont.gy}")
            # draw(f"GZ: {cont.gz}")

            draw_stick(screen, 300, 500, (cont.l_stick[0], cont.l_stick[1]))
            draw_stick(screen, 600, 700, (cont.r_stick[0], cont.r_stick[1]))

        pygame.display.flip()
        clock.tick(240)

    if cont and cont.device:
        cont.device.close()
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
