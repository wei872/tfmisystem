# version:1.0.2510.9101

import gxipy as gx
from gxipy.gxidef import *
from ctypes import *

def main():
    try:
        # create a device manager
        device_manager = gx.DeviceManager()
        dev_num, dev_info_list = device_manager.update_all_device_list()
        if dev_num == 0:
            print("No device!")
            print("<App Exit!>")
            return

        # open the first device
        cam = device_manager.open_device_by_index(1)
        remote_device_feature = cam.get_remote_device_feature_control()

        # Restore default parameter group
        remote_device_feature.get_enum_feature("UserSetSelector").set("Default")
        remote_device_feature.get_command_feature("UserSetLoad").send_command()

        print("***********************************************")
        print(f"<Vendor Name:    {dev_info_list[0]['vendor_name']}>")
        print(f"<Model Name  :    {dev_info_list[0]['model_name']}>")
        print(f"<Serial Number:    {dev_info_list[0]['sn']}>")
        print("***********************************************")

        # Check if the current device supports lossless compression
        is_implemented = remote_device_feature.is_implemented('ImageCompressionMode')
        if not is_implemented:
            print("This device  does not support compression function!")
            print("<App Exit!>")
            return

        is_readable = remote_device_feature.is_readable('ImageCompressionMode')
        is_writable = remote_device_feature.is_writable('ImageCompressionMode')
        if not is_readable or not is_writable:
            print("This device does not support lossless compression function!")
            print("<App Exit!>")
            return

        # Open 'Lossless' mode
        remote_device_feature.get_enum_feature('ImageCompressionMode').set('Lossless')

        # Get decompression param
        img_width = remote_device_feature.get_int_feature('Width').get()
        img_height = remote_device_feature.get_int_feature('Height').get()
        img_pixel_format = remote_device_feature.get_enum_feature('PixelFormat').get()[0]
        img_compression_method = remote_device_feature.get_enum_feature('ImageCompressionMethod').get()[0]
        img_payloadsize = remote_device_feature.get_int_feature('PayloadSize').get()

        # create decompressor object
        obj_decompressor = device_manager.create_decompressor()

        # create decompression buffer
        decompression_image_array = (c_ubyte * img_payloadsize)()
        decompression_image_address = addressof(decompression_image_array)

        # Acquisition and decompress 10 images
        image_num = 10

        # open stream and acquisition start
        cam.stream_on()
        remote_device_feature.get_command_feature('AcquisitionStart').send_command()

        while image_num > 0:
            image_num -= 1
            try:
                image = cam.data_stream[0].dq_buf(1000)
                if image.frame_data.status == GxFrameStatusList.SUCCESS:
                    decompression_image_size = img_payloadsize

                    # Decompression image
                    try:
                        image_size = image.get_image_size()
                        obj_decompressor.decompression(image.frame_data.image_buf, image_size,
                                                       decompression_image_address,
                                                       decompression_image_size, img_pixel_format, img_width,
                                                       img_height,
                                                       img_compression_method)

                        print(f"Frame ID:{image.frame_data.frame_id}    Compression ratio: {(image_size / decompression_image_size):.2f}")

                    except Exception as ex:
                        print(f"error: {str(ex)}")

                else:
                    print("<Abnormal Acquisition>")

                cam.data_stream[0].q_buf(image)

            except Exception as ex:
                print(f"<error: {str(ex)}>")

        # Acquisition stop
        remote_device_feature.get_command_feature('AcquisitionStop').send_command()
        cam.stream_off()

        cam.close_device()

    except Exception as ex:
        print(f"<error: {str(ex)}>")

    print("<App Exit!>")

if __name__ == "__main__":
    main()
