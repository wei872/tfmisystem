#include <iostream>
#include "stdafx.h"
#include "GalaxyIncludes.h"
#include "GxFlatFieldCorrectionProcess.h"

using namespace std;


int main(int argc, char* argv[])
{
    CGXDevicePointer pDevice;
	CGXStreamPointer pStream;
    bool bCreateDevice = false;
	try
	{
		//初始化设备库
		IGXFactory::GetInstance().Init();

		//枚举相机设备
		GxIAPICPP::gxdeviceinfo_vector vectorDeviceInfo;
		IGXFactory::GetInstance().UpdateDeviceList(1000, vectorDeviceInfo);

		//判断当前设备连接个数
		if (vectorDeviceInfo.size() <= 0)
		{
			cout << "No device!" << endl;
			system("pause");
			return 0;
		}

		//通过SN打开相机设备
		pDevice = IGXFactory::GetInstance().OpenDeviceBySN(vectorDeviceInfo[0].GetSN(), GX_ACCESS_EXCLUSIVE);
		bCreateDevice = true;
        //获取相机属性控制对象
		CGXFeatureControlPointer pRemoteControl = pDevice->GetRemoteFeatureControl();
		//流层对象
		if (pDevice->GetStreamCount() > 0)
		{
			pStream = pDevice->OpenStream(0);
		}
		else
		{
            if (bCreateDevice)
            {
				//关闭流
				pStream->Close();
                //关闭相机设备
                pDevice->Close();
            }
                //关闭设备库
                IGXFactory::GetInstance().Uninit();
			cout << "Not find stream!";
			system("pause");
			return 0;
		}

		//选择默认参数组
		pRemoteControl->GetEnumFeature("UserSetSelector")->SetValue("Default");
		//加载参数组
		pRemoteControl->GetCommandFeature("UserSetLoad")->Execute();

		cout << "***********************************************" << endl;
		cout << "<Vendor Name:   " << pDevice->GetDeviceInfo().GetVendorName() << ">" << endl;
		cout << "<Model Name:    " << pDevice->GetDeviceInfo().GetModelName() << ">" << endl;
		cout << "<Serial Number: " << pDevice->GetDeviceInfo().GetSN() << ">" << endl;
		cout << "***********************************************" << endl << endl;

        //1. 创建FFC处理对象
        std::auto_ptr<IFlatFieldCorrectionProcess> pFFCObj = IFlatFieldCorrectionProcess::CreateFlatFieldCorrectionProcess(pStream, pRemoteControl);
		if (pFFCObj.get() == NULL)
		{
            if (bCreateDevice)
            {
				//关闭流
				pStream->Close();
                //关闭相机设备
                pDevice->Close();
            }

            IGXFactory::GetInstance().Uninit();
            cout << "<create flat field correction process error, App exit!>" << endl;
            system("pause");
            return 0;
		}

        //2. 设置平场参数
        GX_FFC_PARAM stFFCParam;
        stFFCParam.nFFCExpectedGray = 127;  //-1 ~255, -1标识用图像块儿的最大值 见说明书
        stFFCParam.nFFCFrameCount = 1;  //1,2,4,8,16 标识融合帧数
        //通过调用 pRemoteControl->GetEnumFeature("FFCCoefficient")->GetEnumEntryList()获取所有支持项
		if (pRemoteControl->IsImplemented("FFCCoefficient") && 
			pRemoteControl->IsReadable("FFCCoefficient"))
		{
			stFFCParam.strCoefficient = pRemoteControl->GetEnumFeature("FFCCoefficient")->GetEnumValue().strCurSymbolic;
		}

        //通过调用 pRemoteControl->GetEnumFeature("FFCAccuracy")->GetEnumEntryList()获取所有支持项
		if (pRemoteControl->IsImplemented("FFCAccuracy") &&
			pRemoteControl->IsReadable("FFCAccuracy"))
		{
			stFFCParam.strAccuracy = pRemoteControl->GetEnumFeature("FFCAccuracy")->GetEnumValue().strCurSymbolic;
		}

        //通过调用 pRemoteControl->GetEnumFeature("FFCBlockSize")->GetEnumEntryList()获取
		if (pRemoteControl->IsImplemented("FFCBlockSize") &&
			pRemoteControl->IsReadable("FFCBlockSize"))
		{
			stFFCParam.nFFCBlockSize = pRemoteControl->GetEnumFeature("FFCBlockSize")->GetEnumValue().nCurValue;
		}

        stFFCParam.bFFCExpectedGray = true;
        pFFCObj->SetFlatFieldCorrectionParam(stFFCParam);

        //3.计算平场矫正系数 false不采集暗场， true采集暗场,
		//仅FFC_SOFTCAL_SOFTUSE与FFC_SOFTCAL_DEVICEUSE_3140类型支持
        bool bCalulate = pFFCObj->Calculate(false);
		if (!bCalulate)
		{
			if (bCreateDevice)
			{
				//关闭流
				pStream->Close();
				//关闭相机设备
				pDevice->Close();
			}

            IGXFactory::GetInstance().Uninit();
			cout << "<Calculate flat field correction error, App exit!>" << endl;
			system("pause");
			return 0;
		}
		cout << "<Flat-field coefficients calculation completed successfully.>" << endl;

        //4.开启平场校正开关
		bool bEnableFFC = true;
        pFFCObj->EnableFFC(bEnableFFC);
		cout << "<Enable flat-field correction.>" << endl;

        //5.获取平场校正后的图像
        CImageDataPointer pImageData = pFFCObj->GetFFCImage();
        
		if (!pImageData.IsNull())
		{
			if (bEnableFFC)
			{
				cout << "<App get FFC Image Success!>" << endl;
			}
			else
			{
				cout << "<App get normal Image Success!>" << endl;
			}	
		}

		//6. 可选保存平场矫正系数，加载平场矫正系数
		//注意FFC_DEVICECAL_DEVICEUSE类相机 当"FFCAccuracy" 设置为PixelLevel时 保存时间较长
		//当保存路径传空时，如果相机支持则平场系数将保存到相机内部, 返回值为是否成功

        // 建议以管理员身份启动程序，防止当前应用程序处于系统盘时因没有管理员权限导致保存失败！
		bool bSaveSuccess = pFFCObj->SaveFFC("FlatFieldCorrectionProcess.ffc");

		bool bLoadSuccess = pFFCObj->LoadFFC("FlatFieldCorrectionProcess.ffc");
		
		//关闭相机设备
		pDevice->Close();
		//关闭设备库
		IGXFactory::GetInstance().Uninit();
	}
	catch (CGalaxyException &e)
	{
        if (bCreateDevice)
        {
			//关闭流
			pStream->Close();
            //关闭相机设备
            pDevice->Close();
        }

        IGXFactory::GetInstance().Uninit();
		cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
	}
	catch (std::exception &e)
	{

        if (bCreateDevice)
        {
			//关闭流
			pStream->Close();
            //关闭相机设备
            pDevice->Close();
        }

        IGXFactory::GetInstance().Uninit();
		cout << "<" << e.what() << ">" << endl;
	}

	cout << "<App exit!>" << endl;
	system("pause");

	return 0;
}