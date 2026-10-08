using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using GxIAPINET;

namespace GxDecompression
{
    class Sample
    {
        static IntPtr m_pDeompressionBuf = IntPtr.Zero;
        static void Main(string[] args)
        {
            try
            {
                // initialization
                IGXFactory.GetInstance().Init();

                // Enumerating cameras
                List<IGXDeviceInfo> listGxDeviceInfo = new List<IGXDeviceInfo>();
                IGXFactory.GetInstance().UpdateAllDeviceList(300, listGxDeviceInfo);
                if (listGxDeviceInfo.Count < 1)
                {
                    Console.WriteLine("Device not found");
                    IGXFactory.GetInstance().Uninit();
                    Console.WriteLine("<App exit!>");
                    Console.WriteLine("Press any key to exit...");
                    Console.ReadKey();
                    return;
                }

                string deviceSN = listGxDeviceInfo[0].GetSN();
                IGXDevice objDevice = IGXFactory.GetInstance().OpenDeviceBySN(deviceSN, GX_ACCESS_MODE.GX_ACCESS_CONTROL);
                IGXFeatureControl objRemoteFeatureControl = objDevice.GetRemoteFeatureControl();

                // Restore default parameter group
                objRemoteFeatureControl.GetEnumFeature("UserSetSelector").SetValue("Default");
                objRemoteFeatureControl.GetCommandFeature("UserSetLoad").Execute();

                Console.WriteLine("***********************************************");
                Console.WriteLine("<Vendor Name:   {0}>", objDevice.GetDeviceInfo().GetVendorName());
                Console.WriteLine("<Model Name:    {0}>", objDevice.GetDeviceInfo().GetModelName());
                Console.WriteLine("<Serial Number: {0}>", objDevice.GetDeviceInfo().GetSN());
                Console.WriteLine("***********************************************");

                if ( !objRemoteFeatureControl.IsImplemented("ImageCompressionMode")
                ||!objRemoteFeatureControl.IsReadable("ImageCompressionMode")
                || !objRemoteFeatureControl.IsWritable("ImageCompressionMode"))
                {
                    IGXFactory.GetInstance().Uninit();

                    Console.WriteLine("This device  does not support compression function!");
			        Console.WriteLine("<App exit!>");
                    Console.WriteLine("Press any key to exit...");
                    Console.ReadKey();
			        return;
                }

                // Open lossless
                objRemoteFeatureControl.GetEnumFeature("ImageCompressionMode").SetValue("Lossless");

                // Get decompression param
                ulong nImgWidth = (ulong)objRemoteFeatureControl.GetIntFeature("Width").GetValue();
                ulong nImgHeight = (ulong)objRemoteFeatureControl.GetIntFeature("Height").GetValue();
                ulong nPayloadSize = (ulong)objRemoteFeatureControl.GetIntFeature("PayloadSize").GetValue();
                GX_PIXEL_FORMAT_ENTRY nPixelFormat = (GX_PIXEL_FORMAT_ENTRY)objRemoteFeatureControl.GetEnumFeature("PixelFormat").GetEnumValue().nCurValue;
                Int32 nMethod = (Int32)objRemoteFeatureControl.GetEnumFeature("ImageCompressionMethod").GetEnumValue().nCurValue;

                IGXDecompressor objDecompressor = IGXFactory.GetInstance().CreateDecompressor();

                // Open the specified flow channel
                IGXStream objStream = objDevice.OpenStream(0);
                objStream.StartGrab();
                objRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();

                m_pDeompressionBuf = Marshal.AllocHGlobal((int)nPayloadSize);

                int nImageNum = 10;
                while( nImageNum-- > 0)
                {
                    try
                    {
                        ulong ulDeompressionBufSize = nPayloadSize;
                        IFrameData frameData = objStream.DQBuf(1000);
                        if (frameData.GetStatus() == GX_FRAME_STATUS_LIST.GX_FRAME_STATUS_SUCCESS)
                        {
                            // Decompression image
                            objDecompressor.Decompression(frameData.GetBuffer(), frameData.GetPayloadSize(), m_pDeompressionBuf, ref ulDeompressionBufSize,
                            nPixelFormat, nImgWidth, nImgHeight, nMethod);

                            Console.WriteLine("FrameID: {0, 3}   CompressionRate: {1, 2:f2}", frameData.GetFrameID(), (float)frameData.GetPayloadSize() / (float)ulDeompressionBufSize);
                        }
                        else
                        {
                            Console.WriteLine("Abnormal Acquisition: Exception code: {0}", frameData.GetStatus());
                        }


                        objStream.QBuf(frameData);
                    }
                    catch (CGalaxyException ex)
                    {
                        Console.WriteLine("<GalaxyException: " + ex.Message + ">");
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine("<Exception: " + ex.Message + ">");
                    }
                }

                objRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                objStream.StopGrab();
                objStream.Close();
                objDevice.Close();
                IGXFactory.GetInstance().Uninit();
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<GalaxyException: " + ex.Message + ">");
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Exception: " + ex.Message + ">");
            }

            if (IntPtr.Zero != m_pDeompressionBuf)
            {
                Marshal.FreeHGlobal(m_pDeompressionBuf);
                m_pDeompressionBuf = IntPtr.Zero;
            }

            Console.WriteLine("<App exit!>");
            Console.WriteLine("Press any key to exit...");
            Console.ReadKey();
            return;
        }
    }
}