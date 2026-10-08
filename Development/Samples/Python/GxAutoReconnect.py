# version:1.0.2406.9261

import gxipy as gx
from gxipy.gxidef import *
import threading
import sys

def reconnect_callback():
    print("The reconnect callback is triggered!")

def disconnect_callback():
    print("The disconnect callback is triggered!")


def main():
    # create a device manager
    device_manager = gx.DeviceManager()
    dev_num, dev_info_list = device_manager.update_all_device_list()
    if dev_num == 0:
        print("Number of enumerated devices is 0")
        return

    # open the first device
    cam = device_manager.open_device_by_index(1)
    remote_device_feature = cam.get_remote_device_feature_control()

    local_device_feature = cam.get_local_device_feature_control()
    if not local_device_feature.is_implemented("EnableAutoConnection") or not local_device_feature.is_writable("EnableAutoConnection"):
        print("The camera does not support disconnection and reconnection")
        cam.close_device()
        return 
        
    # Restore default parameter group
    remote_device_feature.get_enum_feature("UserSetSelector").set("Default")
    remote_device_feature.get_command_feature("UserSetLoad").send_command()

    cam.stream_on()

    #Register disconnection and reconnection callback functions
    cam.register_device_reconnect_callback(reconnect_callback)
    cam.register_device_disconnect_callback(disconnect_callback)

    local_device_feature.get_bool_feature("EnableAutoConnection").set(True)

    print("请手动插拔相机触发掉线，测试完成后点击回车完成测试!")
    sys.stdin.read(1)

    local_device_feature.get_bool_feature("EnableAutoConnection").set(False)
    cam.unregister_device_reconnect_callback()
    cam.unregister_device_disconnect_callback()
    
    cam.stream_off()
    cam.close_device()


if __name__ == "__main__":
    main()
