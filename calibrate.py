import time
import json
import main

manager = main.HIDControllerManager()

while not manager.controllers:
    manager.open_devices()
    time.sleep(0.1)

# get raw data from the controller
manager.controllers[0].set_raw(True)

manager.controllers[0].update()
maxx = manager.controllers[0].stick[0]
maxy = manager.controllers[0].stick[1]
minx = manager.controllers[0].stick[0]
miny = manager.controllers[0].stick[1]
try:
    while True:
        manager.controllers[0].update()
        maxx = max(maxx, manager.controllers[0].stick[0])
        maxy = max(maxy, manager.controllers[0].stick[1])
        minx = min(minx, manager.controllers[0].stick[0])
        miny = min(miny, manager.controllers[0].stick[1])
        time.sleep(0.016)
except:
    # user interrupt
    print("Done")
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
