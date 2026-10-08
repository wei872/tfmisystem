#include "stdafx.h"
#include <iostream>
#include "GalaxyIncludes.h"

using namespace std;


//--------------------------------------------------
/**
\brief     用户继承断线处理类
*/
//--------------------------------------------------
class DeviceDisconnectEventHandler : public IDeviceDisconnectEventHandler
{
public:
	virtual ~DeviceDisconnectEventHandler(void) {};

	//--------------------------------------------------
	/**
	\brief     断线回调函数
	\param     pUserParam      用户参数

	\return    void
	*/
	//--------------------------------------------------
	virtual void DoOnDeviceDisconnectEvent(void* pUserParam)
	{
		std::cout << "The disconnect event is triggered!" << std::endl;
	}
};

//--------------------------------------------------
/**
\brief     用户继承重连处理类
*/
//--------------------------------------------------
class DeviceReconnectEventHandler : public IDeviceReconnectEventHandler
{
public:
	virtual ~DeviceReconnectEventHandler(void) {};

	//--------------------------------------------------
	/**
	\brief     重连回调函数
	\param     pUserParam      用户参数

	\return    void
	*/
	//--------------------------------------------------
	virtual void DoOnDeviceReconnectEvent(void* pUserParam)
	{
		std::cout << "The reconnect event is triggered!" << std::endl;
	}
};


int main(int argc, char* argv[])
{
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
			return 0;
		}

		//通过SN打开相机设备
		CGXDevicePointer pDevice = IGXFactory::GetInstance().OpenDeviceBySN(vectorDeviceInfo[0].GetSN(), GX_ACCESS_EXCLUSIVE);
		//获取相机属性控制对象
		CGXFeatureControlPointer pRemoteControl = pDevice->GetRemoteFeatureControl();
		//流层对象
		CGXStreamPointer pStream;
		if (pDevice->GetStreamCount() > 0)
		{
			pStream = pDevice->OpenStream(0);
		}
		else
		{
			cout << "Not find stream!";
			return 0;
		}

		//选择默认参数组
		pRemoteControl->GetEnumFeature("UserSetSelector")->SetValue("Default");
		//加载参数组
		pRemoteControl->GetCommandFeature("UserSetLoad")->Execute();

		bool bImplemented = pDevice->GetFeatureControl()->IsImplemented("EnableAutoConnection");
		if (!bImplemented)
		{
			printf("该相机不支持断线重连功能!\n");
			//关闭流
			pStream->Close();
			//关闭相机设备
			pDevice->Close();
			//关闭设备库
			IGXFactory::GetInstance().Uninit();
			return 0;
		}

		//开启流层采集
		pStream->StartGrab();
		
		//开启相机采集
		pRemoteControl->GetCommandFeature("AcquisitionStart")->Execute();

		DeviceReconnectEventHandler hReconnectHandle;
		DeviceDisconnectEventHandler hDisconnectHandle;

		//注册重连和掉线回调
		pDevice->RegisterDeviceReconnectCallback(&hReconnectHandle, NULL);
		pDevice->RegisterDeviceDisconnectCallback(&hDisconnectHandle, NULL);


		pDevice->GetFeatureControl()->GetBoolFeature("EnableAutoConnection")->SetValue(true);

		printf("请手动插拔相机触发掉线，测试完成后点击回车完成测试!\n");
		getchar();

		//相机停止采集
		pRemoteControl->GetCommandFeature("AcquisitionStop")->Execute();
		//流层停止采集
		pStream->StopGrab();

		pDevice->UnregisterDeviceDisconnectCallback();
		pDevice->UnregisterDeviceReconnectCallback();

		//关闭流
		pStream->Close();
		//关闭相机设备
		pDevice->Close();
		//关闭设备库
		IGXFactory::GetInstance().Uninit();
	}
	catch (CGalaxyException &e)
	{
		cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
	}
	catch (std::exception &e)
	{
		cout << "<" << e.what() << ">" << endl;
	}

	cout << "<App exit!>" << endl;

	return 0;
}