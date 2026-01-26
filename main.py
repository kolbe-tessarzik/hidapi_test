import hid
import pygame
import sys
import time

NINTENDO_VID = 0x057E

WIDTH, HEIGHT = 900, 900

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
            cont.send_subcommand(cont.device, 0x03, bytes([0x30]))
            opened.append(cont)
        self.controllers += opened
        return opened

class HIDController:
    def __init__(self, device, info):
        self.device = device
        self.info = info
        self.packet_num = 0
        self._recent_data = None

        self.r_stick = (0, 0)
        self.l_stick = (0, 0)

    def send_subcommand(self, dev, subcmd, payload=b""):
        # 49-byte output report (Bluetooth)
        report = bytearray(49)

        report[0] = 0x01                     # Output report ID
        report[1] = self.packet_num & 0x0F   # Packet counter (0–15)

        # Bytes 2..9 = rumble data (leave zero)
        report[10] = subcmd
        report[11:11+len(payload)] = payload

        self.packet_num += 1
        dev.write(bytes(report))

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


    def update(self):
        try:
            self._recent_data = self.device.read(64, timeout=0)
        except hid.HIDException as e:
            print("Device timed out")
            return
        # 10/11 = r stick X/Y
        if self._recent_data:
            self.r_stick = self.right_stick(self._recent_data)
            self.l_stick = self.left_stick(self._recent_data)

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
        time.sleep(1)
        manager.open_devices()

    cont, info = manager.controllers[0], manager.controllers[0].info

    last_packet = None
    last_packet_time = 0

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
