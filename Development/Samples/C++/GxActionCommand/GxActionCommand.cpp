#include "stdafx.h"
#include "GalaxyIncludes.h"
#include <iostream>
#include <vector>

//检查是否所有相机都支持ActionCommand和ptp功能
void CheckCamParameters(std::vector<CGXDevicePointer>& vectorDevPtr, std::vector<CGXFeatureControlPointer>& vectorRemoteControl)
{
    std::cout << "check is all device support ActionCommand and PTP" << std::endl;

    bool bIsAllDevSupport = true;
    for (int i = 0; i < (int)vectorRemoteControl.size(); i++)
    {
        gxstring_vector vecEnumValue = vectorRemoteControl[i]->GetEnumFeature("GevSupportedOptionSelector")->GetEnumEntryList();

        bool bActionItemExist = false;
        bool bScheduledActionItemExist = false;
        bool bPtpItemExist = false;

        bool bActionSupport = false;
        bool bScheduledActionSupport = false;
        bool bPtpSupport = false;

        for (int j = 0; j < vecEnumValue.size(); j++)
        {
            if (strcmp(vecEnumValue[j].c_str(), "Action") == 0)
            {
                bActionItemExist = true;
            }

            if (strcmp(vecEnumValue[j].c_str(), "ScheduledAction") == 0)
            {
                bScheduledActionItemExist = true;
            }

            if (strcmp(vecEnumValue[j].c_str(), "Ptp") == 0)
            {
                bPtpItemExist = true;
            }
        }

        if (bActionItemExist && bScheduledActionItemExist && bPtpItemExist)
        {
            vectorRemoteControl[i]->GetEnumFeature("GevSupportedOptionSelector")->SetValue("Action");

            bActionSupport = vectorRemoteControl[i]->GetBoolFeature("GevSupportedOption")->GetValue();

            vectorRemoteControl[i]->GetEnumFeature("GevSupportedOptionSelector")->SetValue("ScheduledAction");

            bScheduledActionSupport = vectorRemoteControl[i]->GetBoolFeature("GevSupportedOption")->GetValue();

            vectorRemoteControl[i]->GetEnumFeature("GevSupportedOptionSelector")->SetValue("Ptp");

            bPtpSupport = vectorRemoteControl[i]->GetBoolFeature("GevSupportedOption")->GetValue();

            if (bActionSupport && bScheduledActionSupport && bPtpSupport)
            {
                //当前相机支持Action ScheduledAction Ptp
            }
            else
            {
                gxstring strSN = vectorDevPtr[i]->GetDeviceInfo().GetSN();
                std::cout << "SN:" << strSN.c_str() << " don't support ActionCommand or PTP" << std::endl;
                bIsAllDevSupport = false;
            }
        }
        else
        {
            gxstring strSN = vectorDevPtr[i]->GetDeviceInfo().GetSN();
            std::cout << "SN:" << strSN.c_str() << " don't support ActionCommand or PTP" << std::endl;
            bIsAllDevSupport = false;
        }
    }

    if (!bIsAllDevSupport)
    {
        throw CGalaxyException(GX_STATUS_ERROR, "not all cam support ActionCommand and PTP");
    }
    else
    {
        std::cout << "successful check, all device support ActionCommand and PTP" << std::endl;
    }
}

//设置相机参数并开采
void SetCamParametersAndStartAcquisition(std::vector<CGXDevicePointer>& vectorDevPtr
    , std::vector<CGXFeatureControlPointer>& vectorRemoteControl
    , std::vector<CGXStreamPointer>& vectorStream)
{
    std::cout << "setting cam ActionCommand parameters" << std::endl;

    for (int i = 0; i < (int)vectorRemoteControl.size(); i++)
    {
        //选择默认参数组
        vectorRemoteControl[i]->GetEnumFeature("UserSetSelector")->SetValue("Default");

        //加载参数组
        vectorRemoteControl[i]->GetCommandFeature("UserSetLoad")->Execute();

        //开启触发模式
        vectorRemoteControl[i]->GetEnumFeature("TriggerMode")->SetValue("On");

        //触发源设置为Action0
        vectorRemoteControl[i]->GetEnumFeature("TriggerSource")->SetValue("Action0");

        //设置相机ActionCommand参数
        vectorRemoteControl[i]->GetIntFeature("ActionDeviceKey")->SetValue(1);

        vectorRemoteControl[i]->GetIntFeature("ActionGroupKey")->SetValue(1);

        vectorRemoteControl[i]->GetIntFeature("ActionGroupMask")->SetValue(0xFFFFFFFF);

        //相机开始采集
        vectorStream[i]->StartGrab();

        vectorRemoteControl[i]->GetCommandFeature("AcquisitionStart")->Execute();
    }

    std::cout << "setting success" << std::endl;
}

//演示ActionCommand命令
void ShowActionCommand(std::vector<CGXDevicePointer>& vectorDevPtr
    , std::vector<CGXStreamPointer>& vectorStream
    , GX_GIGE_ACTION_COMMAND_RESULT** pResultBuffer)
{
    std::cout << "demonstrate ActionCommand function" << std::endl;

    uint32_t ui32DeviceKey = 1;
    uint32_t ui32GroupKey = 1;
    uint32_t ui32GroupMask = 0xFFFFFFFF;
    gxstring strSpecialIP = "";
    uint32_t ui32NumResult = (uint32_t)vectorDevPtr.size();
    *pResultBuffer = new GX_GIGE_ACTION_COMMAND_RESULT[ui32NumResult];

    //strBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
    gxstring strIP = "255.255.255.255";

    IGXFactory::GetInstance().GigEIssueActionCommand(ui32DeviceKey, ui32GroupKey, ui32GroupMask
        , strIP, strSpecialIP, 500, &ui32NumResult, *pResultBuffer);

    //打印结果
    for (uint32_t i = 0; i < ui32NumResult; i++)
    {
        std::cout << "Ack Return " << " ip:" << (*pResultBuffer)[i].DeviceAddress
            << " status:" << (*pResultBuffer)[i].nStatus << std::endl;
    }

    //接收图像
    for (int i = 0; i < (int)vectorDevPtr.size(); i++)
    {
        CImageDataPointer objImagePtr;
        objImagePtr = vectorStream[i]->DQBuf(1000);
        std::cout << "SN:" << vectorDevPtr[i]->GetDeviceInfo().GetSN() << " get image success,"
            << " image status:" << ((objImagePtr->GetStatus() == GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame")
            << std::endl;
        vectorStream[i]->QBuf(objImagePtr);
    }
}

//演示ScheduledActionCommand命令
void ShowScheduledActionCommand(std::vector<CGXDevicePointer>& vectorDevPtr
    , std::vector<CGXFeatureControlPointer>& vectorRemoteControl
    , std::vector<CGXStreamPointer>& vectorStream
    , GX_GIGE_ACTION_COMMAND_RESULT** pResultBuffer)
{
    uint32_t ui32DeviceKey = 1;
    uint32_t ui32GroupKey = 1;
    uint32_t ui32GroupMask = 0xFFFFFFFF;
    gxstring strSpecialIP = "";
    uint32_t ui32NumResult = (uint32_t)vectorDevPtr.size();

    //strBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
    gxstring strIP = "255.255.255.255";

    //设置PTP参数
    std::cout << "setting cam PTP parameters" << std::endl;

    for (int i = 0; i < (int)vectorRemoteControl.size(); i++)
    {
        //打开相机PTP功能
        vectorRemoteControl[i]->GetBoolFeature("PtpEnable")->SetValue(true);
    }

    //首先应该等待相机分配角色，需要时间约8s，循环读取PtpStatus，直到值为"Master"或"Slave"时，角色分配完成
    //然后进行时间校准，精度到1μs内需要时间约1~2min，循环设置Slave相机的PtpDataSetLatch，并读取PtpOffsetFromMaster，
    //即可获得Slave相对于Master的时间偏差，当PtpOffsetFromMaster的绝对值小于用户期望的时间精度，时间校准完成
    gxstring strCam0PtpStatus = vectorRemoteControl[0]->GetEnumFeature("PtpStatus")->GetValue();

    int i32Loops = 0;
    bool bStatusOK = (strcmp(strCam0PtpStatus.c_str(), "Master") == 0) || (strcmp(strCam0PtpStatus.c_str(), "Slave") == 0);

    while (!bStatusOK && i32Loops < 8)
    {
        Sleep(1000);
        i32Loops++;

        strCam0PtpStatus = vectorRemoteControl[0]->GetEnumFeature("PtpStatus")->GetValue();

        bStatusOK = (strcmp(strCam0PtpStatus.c_str(), "Master") == 0) || (strcmp(strCam0PtpStatus.c_str(), "Slave") == 0);
    }

    if (!bStatusOK)
    {
        throw CGalaxyException(GX_STATUS_ERROR, "PTP time calibration timeout");
    }

    std::cout << "setting success" << std::endl;

    //演示ScheduledActionCommand命令
    std::cout << "demonstrate ScheduledActionCommand function" << std::endl;

    //获取相机当前时间戳，单位ns，计划5s后相机采一张图像
    vectorRemoteControl[0]->GetCommandFeature("TimestampLatch")->Execute();
    int64_t i64TimeStamp = vectorRemoteControl[0]->GetIntFeature("TimestampLatchValue")->GetValue();
    i64TimeStamp += 5000000000;

    IGXFactory::GetInstance().GigEIssueScheduledActionCommand(ui32DeviceKey, ui32GroupKey, ui32GroupMask
        , i64TimeStamp, strIP, strSpecialIP, 500, &ui32NumResult, *pResultBuffer);

    //等待相机执行
    Sleep(5000);

    //打印结果
    for (uint32_t i = 0; i < ui32NumResult; i++)
    {
        std::cout << "Ack Return " << " ip:" << (*pResultBuffer)[i].DeviceAddress
            << " status:" << (*pResultBuffer)[i].nStatus
            << std::endl;
    }

    //接收图像
    for (int i = 0; i < (int)vectorDevPtr.size(); i++)
    {
        CImageDataPointer objImagePtr;
        objImagePtr = vectorStream[i]->DQBuf(1000);
        std::cout << "SN:" << vectorDevPtr[i]->GetDeviceInfo().GetSN() << " get image success,"
            << " image status:" << ((objImagePtr->GetStatus() == GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame")
            << std::endl;
        vectorStream[i]->QBuf(objImagePtr);
    }
}

//停采并关闭相机
void StopAcquisitionAndCloseCam(std::vector<CGXDevicePointer>& vectorDevPtr
    , std::vector<CGXFeatureControlPointer>& vectorRemoteControl
    , std::vector<CGXStreamPointer>& vectorStream)
{
    //停止采集关闭相机
    for (int i = 0; i < (int)vectorRemoteControl.size(); i++)
    {
        try
        {
            vectorRemoteControl[i]->GetCommandFeature("AcquisitionStop")->Execute();
        }
        catch (...)
        {
            std::cout << "cam idx:" << i << " stop acquisition fail!" << std::endl;
            continue;
        }
    }

    for (int i = 0; i < (int)vectorStream.size(); i++)
    {
        try
        {
            vectorStream[i]->Close();
        }
        catch (...)
        {
            std::cout << "cam idx:" << i << " close stream fail!" << std::endl;
            continue;
        }
    }

    for (int i = 0; i < (int)vectorDevPtr.size(); i++)
    {
        try
        {
            vectorDevPtr[i]->Close();
        }
        catch (...)
        {
            std::cout << "cam idx:" << i << " close device fail!" << std::endl;
            continue;
        }
    }
}

int main(int argc, char* argv[])
{
    std::vector<CGXDevicePointer>         vectorDevPtr;
    std::vector<CGXFeatureControlPointer> vectorRemoteControl;
    std::vector<CGXStreamPointer>         vectorStream;
    GX_GIGE_ACTION_COMMAND_RESULT*        pResultBuffer = NULL;

	try
	{
		//初始化设备库
		IGXFactory::GetInstance().Init();

		//枚举网络相机设备
		GxIAPICPP::gxdeviceinfo_vector vectorDeviceInfo;
        IGXFactory::GetInstance().UpdateAllDeviceListEx(GX_TL_TYPE_GEV, 1000, vectorDeviceInfo);

		//判断当前设备连接个数
		if (vectorDeviceInfo.size() < 1)
		{
            throw CGalaxyException(GX_STATUS_ERROR, "Gige device less than 1!");
		}

        std::cout << "open device" << std::endl;

        for (int i = 0; i < (int)vectorDeviceInfo.size(); i++)
        {
            //通过SN打开所有枚举到的网络相机设备
            CGXDevicePointer pDevice = IGXFactory::GetInstance().OpenDeviceBySN(vectorDeviceInfo[i].GetSN(), GX_ACCESS_EXCLUSIVE);
            vectorDevPtr.push_back(pDevice);

            //获取相机属性控制对象
            CGXFeatureControlPointer pRemoteControl = pDevice->GetRemoteFeatureControl();
            vectorRemoteControl.push_back(pRemoteControl);

            //流层对象
            CGXStreamPointer pStream;
            if (pDevice->GetStreamCount() > 0)
            {
                pStream = pDevice->OpenStream(0);
                vectorStream.push_back(pStream);
            }
            else
            {
                throw CGalaxyException(GX_STATUS_ERROR, "Not find stream!");
            }

            std::cout << "<idx:" << i 
                << "> <Model Name:" << vectorDeviceInfo[i].GetModelName()
                << "> <Serial Number:" << vectorDeviceInfo[i].GetSN() << ">" 
                << std::endl;
        }

        //检查是否所有相机都支持ActionCommand和ptp功能
        CheckCamParameters(vectorDevPtr, vectorRemoteControl);

        //设置相机参数
        SetCamParametersAndStartAcquisition(vectorDevPtr, vectorRemoteControl, vectorStream);
        
        //演示ActionCommand命令 GxActionCommand
        ShowActionCommand(vectorDevPtr, vectorStream, &pResultBuffer);
        
        //演示ScheduledActionCommand命令
        ShowScheduledActionCommand(vectorDevPtr, vectorRemoteControl, vectorStream, &pResultBuffer);
        
        //停止采集并关闭相机
        StopAcquisitionAndCloseCam(vectorDevPtr, vectorRemoteControl, vectorStream);

		//关闭设备库
		IGXFactory::GetInstance().Uninit();
	}
	catch (CGalaxyException &e)
	{
		std::cout << "<Get Galaxy Exception:" << e.GetErrorCode() 
            << ">" << "<" << e.what() << ">" << std::endl;

        //停止采集并关闭相机
        StopAcquisitionAndCloseCam(vectorDevPtr, vectorRemoteControl, vectorStream);

        //关闭设备库
        IGXFactory::GetInstance().Uninit();
	}
	catch (std::exception &e)
	{
		std::cout << "<Get Unknow Error:" << e.what() << ">" << std::endl;

        //关闭设备库
        IGXFactory::GetInstance().Uninit();
	}

    if (pResultBuffer != NULL)
    {
        delete[] pResultBuffer;
    }

	std::cout << "App exit!" << std::endl;
	system("pause");

	return 0;
}