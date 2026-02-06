import pygame
import time
import json
import sys
import main

pygame.display.init()
pygame.font.init()

screen = pygame.display.set_mode((400, 400))
pygame.display.set_caption("Custom Controller Calibration")

font = pygame.font.SysFont("Arial", 30)

clock = pygame.time.Clock()

manager = main.HIDControllerManager()

while not manager.controllers:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            sys.exit()
    manager.open_devices()
    clock.tick(60)

# get raw data from the controller
manager.controllers[0].set_raw(True)

manager.controllers[0].update()
maxx = manager.controllers[0].stick[0]
maxy = manager.controllers[0].stick[1]
minx = manager.controllers[0].stick[0]
miny = manager.controllers[0].stick[1]

msg_surf = font.render("Rotate the joystick in a circle", True, (255, 255, 255), (0, 0, 0))
press_space_surf = font.render("Then press space", True, (255, 255, 255), (0, 0, 0))
rotate_surf = pygame.transform.scale(pygame.image.load("circle-joystick.png"), (200, 200))

rotate_animation = [
    pygame.transform.rotate(rotate_surf, -0),
    pygame.transform.rotate(rotate_surf, -45),
    pygame.transform.rotate(rotate_surf, -90),
    pygame.transform.rotate(rotate_surf, -135),
]

rotate_index = 0

last_rotate = time.time()
start_time = time.time()

done = False
while not done:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            sys.exit(0)
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_SPACE:
                done = True
    screen.fill((0, 0, 0))
    screen.blit(msg_surf, (0, 0))
    if time.time() - start_time > 3:
        screen.blit(press_space_surf, (0, 300))
    if time.time() - last_rotate > 1:
        rotate_index = (rotate_index + 1) % len(rotate_animation)
        last_rotate = time.time()
    current_surf = rotate_animation[rotate_index]
    screen.blit(current_surf, (200 - current_surf.get_width() // 2, 200 - current_surf.get_height() // 2))
    manager.controllers[0].update()
    maxx = max(maxx, manager.controllers[0].stick[0])
    maxy = max(maxy, manager.controllers[0].stick[1])
    minx = min(minx, manager.controllers[0].stick[0])
    miny = min(miny, manager.controllers[0].stick[1])
    pygame.display.flip()
    clock.tick(60)
# create with {} if file doesn't exist
try:
    with open("cal.json", "x") as f:
        f.write("{}")
except FileExistsError:
    print("File already exists")
with open("cal.json", "r") as f:
    cal_data = json.load(f)

with open("cal.json", "w") as f:
    cal_data[manager.controllers[0].serial] = {
        "center": None,
        "max"   : (maxx, maxy),
        "min"   : (minx, miny),
    }
    json.dump(cal_data, f, indent=4)
    print("Calibration complete")

screen.fill((0, 0, 0))
screen.blit(font.render("Thank you!", True, (255, 255, 255), (0, 0, 0)), (0, 0))
screen.blit(font.render("Calibration complete!", True, (255, 255, 255), (0, 0, 0)), (0, 50))
pygame.display.flip()
time.sleep(0.25)
pygame.quit()
