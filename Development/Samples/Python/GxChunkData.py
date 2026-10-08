# version:1.0.2406.9261
import threading

import gxipy as gx
from gxipy.gxidef import *
import time


def get_image_thread(cam, arg_event_flag):
    cam.stream_on()
    while not arg_event_flag.is_set():
        try:
            image = cam.data_stream[0].dq_buf(1000)
            if image.frame_data.status == GxFrameStatusList.SUCCESS:
                chunk_data_feature_ctl = image.get_chunk_data_feature_control()

                chunk_id = chunk_data_feature_ctl.get_int_feature("ChunkFrameID").get()
                print(f"<Successful acquisition: Width: {image.frame_data.width}, " +
                      f"Height: {image.frame_data.height}, ChunkFrameID: {chunk_id}>")
            else:
                print("<Abnormal Acquisition, error code:%d>" % image.frame_data.status)
            cam.data_stream[0].q_buf(image)
            time.sleep(1)
        except Exception as ex:
            print(f"error: {str(ex)}")

    cam.stream_off()


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

    # Restore default parameter group
    remote_device_feature.get_enum_feature("UserSetSelector").set("Default")
    remote_device_feature.get_command_feature("UserSetLoad").send_command()

    # Cameras that do not support frame information do not have the ChunkModeActive function, so they cannot output frame information, so the program ends;
    if not remote_device_feature.is_implemented("ChunkModeActive") or not remote_device_feature.is_writable(
            "ChunkModeActive"):
        # close device
        cam.close_device()
        print("<ChunkData is not supported, App exit!>")
        return

    remote_device_feature.get_bool_feature("ChunkModeActive").set(True)

    #USB interface camera, each frame information item has its own switch, which can be turned on as needed. This sample program only displays the real FrameID;
    #Gev interface camera, currently there is no item switch, only the CHunkModeActive master switch;
    if remote_device_feature.is_implemented("ChunkSelector") and remote_device_feature.is_writable("ChunkSelector"):
        remote_device_feature.get_enum_feature("ChunkSelector").set('FrameID')
        if remote_device_feature.is_implemented("ChunkEnable") and remote_device_feature.is_writable("ChunkEnable"):
            remote_device_feature.get_bool_feature("ChunkEnable").set(True)

    print("***********************************************")
    print(f"<Vendor Name:   {dev_info_list[0]['vendor_name']}>")
    print(f"<Model Name:    {dev_info_list[0]['model_name']}>")
    print("***********************************************")
    print("Press [a] or [A] and then press [Enter] to start acquisition")
    print("Press [x] or [X] and then press [Enter] to Exit the Program")

    wait_start = True
    while wait_start:
        user_input = input()
        if user_input == 'A' or user_input == 'a':
            wait_start = False
        elif user_input == 'X' or user_input == 'x':
            cam.close_device()
            print("<App exit!>")
            return

    event_flag = threading.Event()
    event_flag.clear()

    # Start the collection thread
    thread1 = threading.Thread(target=get_image_thread, args=(cam, event_flag))
    thread1.setDaemon(True)
    thread1.start()

    wait_start = True
    while wait_start:
        user_input = input()
        if user_input == 'X' or user_input == 'x':
            wait_start = False

    # Stop collecting
    event_flag.set()
    thread1.join()

    cam.close_device()


if __name__ == "__main__":
    main()
