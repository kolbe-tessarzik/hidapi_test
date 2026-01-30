import hid
import pygame
import sys
import time
import struct
import math

NINTENDO_VID = 0x057E

WIDTH, HEIGHT = 900, 900

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

class HIDControllerManager:
    def __init__(self):
        self.controllers = []
        self.packet_num = 0

    def find_nintendo_devices(self):
        return [d for d in hid.enumerate() if d["vendor_id"] == NINTENDO_VID]

    def open_devices(self):
        devs = self.find_nintendo_devices()
        opened = []
        for d in devs:
            cont = HIDController(device=hid.Device( path=d["path"] ), info=d)
            # Switch to full 0x30 input reports
            cont.send_subcommand(0x03, bytes([0x30]))
            # Enable tilt controls
            cont.send_subcommand(0x40, bytes([0x01]))
            # enable rumble
            cont.send_subcommand(0x48, bytes([0x01]))
            cont.play_rumble( switch_connect_wave(), frame_delay=0.005 )
            time.sleep(0.1)
            cont.stop_rumble()

            opened.append(cont)
        self.controllers += opened
        return opened

class HIDController:
    def __init__(self, device, info):
        self.device = device
        self.info = info
        self.packet_num = 0
        self._recent_data = None

        self.last_keepalive = time.time()

        self.r_stick = (0, 0)
        self.l_stick = (0, 0)

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
        self.device.write(bytes(report))

    def play_rumble(self, frames, frame_delay=0.015):
        """
        frames: list of 8-byte lists
        frame_delay: seconds per frame (~60Hz)
        """

        for frame in frames:
            report = bytearray(49)
            report[0] = 0x01
            report[1] = self.packet_num & 0x0F
            report[2:10] = bytes(frame)
            report[10] = 0x00
            self.packet_num += 1
            self.device.write(bytes(report))
            time.sleep(frame_delay)

        # stop vibration
        self.send_subcommand(0x00)


    def send_rumble(self):
        report = bytearray(49)
        report[0] = 0x01
        report[1] = self.packet_num & 0x0F

        # Known-good HD rumble pattern (both motors)
        report[2:10] = bytes([
            0x80, 0x00, 0x40, 0x40,   # left motor
            0x80, 0x00, 0x40, 0x40    # right motor
        ])

        # no subcommand
        report[10] = 0x00

        self.packet_num += 1
        self.device.write(bytes(report))

    def rumble_burst(self, duration=0.5):

        end = time.time() + duration

        while time.time() < end:
            report = bytearray(49)
            report[0] = 0x01
            report[1] = self.packet_num & 0x0F

            # Strong, obvious rumble (both motors)
            report[2:10] = bytes([
                0xFF, 0x00, 0xFF, 0xFF,
                0xFF, 0x00, 0xFF, 0xFF
            ])

            report[10] = 0x00

            self.packet_num += 1
            self.device.write(bytes(report))

            time.sleep(0.015)   # ~60 Hz

    def sharp_rumble(self):
        for _ in range(3):  # 3 packets ~50 ms total
            report = bytearray(49)
            report[0] = 0x01
            report[1] = self.packet_num & 0x0F

            # sharp click / snap
            report[2:10] = bytes([0x00, 0x40, 0x80, 0x40,
                                0x00, 0x40, 0x80, 0x40])
            report[10] = 0x00
            self.packet_num += 1
            self.device.write(bytes(report))
            time.sleep(0.015)

        # silence afterward
        self.send_subcommand(0x00)



    def stop_rumble(self):
        self.send_subcommand(0x00)

    def unpack_tilt_controls(self):
        assert(self._recent_data is not None)
        # First IMU sample starts at byte 13
        base = 13

        self.ax, self.ay, self.az, self.gx, self.gy, self.gz = struct.unpack_from("<hhhhhh", self._recent_data, base)



    def scale_axis(self, value, center=2048, min_val=0, max_val=4095):
        """
        Convert raw 12-bit stick value to -100 .. 100 range
        """
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
        return int(scaled)

    def left_stick(self, report):
        """
        report: list or bytes from 0x30 input
        returns: (x, y) tuple scaled -100..100
        """
        # raw 12-bit values
        raw_x = report[6] | ((report[7] & 0x0F) << 8)
        raw_y = ((report[7] >> 4) | (report[8] << 4))

        x = self.scale_axis(raw_x)
        y = -self.scale_axis(raw_y)
        return (x, y)


    def right_stick(self, report):
        """
        report: list or bytes from 0x30 input
        returns: (x, y) tuple scaled -100..100
        """
        raw_x = report[9] | ((report[10] & 0x0F) << 8)
        raw_y = ((report[10] >> 4) | (report[11] << 4))

        x = self.scale_axis(raw_x)
        y = -self.scale_axis(raw_y)
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
                raise

            if not d:
                break

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
            return
        if self._recent_data:
            print(f"Report ID: 0x{self._recent_data[0]:02x}")

        if time.time() - self.last_keepalive > 1.0:
            self.send_subcommand(0x00)  # 0x00 = no-op subcommand
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
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Nintendo Switch Bluetooth HID Demo")

    font = pygame.font.SysFont("monospace", 24)
    clock = pygame.time.Clock()

    manager = HIDControllerManager()
    manager.open_devices()

    while not manager.controllers:
        print("Waiting for Nintendo devices...")
        time.sleep(0.1)
        manager.open_devices()

    cont, info = manager.controllers[0], manager.controllers[0].info

    last_packet = None
    last_packet_time = 0

    try:
        pygame.event.get()
    except SystemError:
        print("First pygame.event.get() falied")

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

        data = cont._recent_data
        if data:
            # Only update when we actually received a packet
            last_packet = bytes(data)

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
        draw(f"AX: {cont.ax}")
        draw(f"AY: {cont.ay}")
        draw(f"AZ: {cont.az}")
        draw(f"GX: {cont.gx}")
        draw(f"GY: {cont.gy}")
        draw(f"GZ: {cont.gz}")

        draw_stick(screen, 200, 500, (cont.l_stick[0], cont.l_stick[1]))
        draw_stick(screen, 600, 700, (cont.r_stick[0], cont.r_stick[1]))

        pygame.display.flip()
        clock.tick(240)

    if cont.device:
        cont.device.close()
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
