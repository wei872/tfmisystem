//------------------------------------------------------------------------
/**
\file		IGXDevice.h
\brief		Definition of the IGXDevice interface
\Date       2023-10-24
\Version    1.1.2310.9241
*/
//------------------------------------------------------------------------
#pragma once
#include "GXIAPIBase.h"
#include "IGXInterface.h"
#include "GXDeviceInfo.h"
#include "GXSmartPtr.h"
#include "IDeviceOfflineEventHandler.h"
#include "IDeviceReconnectEventHandler.h"
#include "IDeviceDisconnectEventHandler.h"
#include "GXStringCPP.h"
#include "IGXStream.h"
#include "IGXFeatureControl.h"
#include "IImageProcessConfig.h"

class GXIAPICPP_API IGXDevice
{

public:
	//---------------------------------------------------------
    /**
    \brief Destructor
    */
    //---------------------------------------------------------
	virtual ~IGXDevice(){};

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns info object which stores the informations of the device, such as the device's name.
	\return   A reference to the device info object
	*/
	//----------------------------------------------------------------------------------
	virtual const CGXDeviceInfo& GetDeviceInfo() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns the number of the stream object that the camera object provides.
	\return   The number of stream object
	*/
	//----------------------------------------------------------------------------------
	virtual uint32_t GetStreamCount() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns a CGXStreamPointer object
	Stream grabbers (CGXStreamPointer) are the objects used for grabbing images
	from a camera device. A camera device might be able to send image data
	over more than one logical channel which is also called stream. A stream grabber grabs
	data from one single stream.

	\param [in]nStreamID   The number of the grabber to return
	\return A CGXStreamPointer object to a stream grabber
	*/
	//----------------------------------------------------------------------------------
	virtual CGXStreamPointer OpenStream(uint32_t nStreamID) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns the set of related local device parameters.
	\return   A CGXFeatureControlPointer object to a feature control.
	*/
	//----------------------------------------------------------------------------------
	virtual CGXFeatureControlPointer GetFeatureControl() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns the set of related remote device parameters.
	\return   A CGXFeatureControlPointer object to a feature control.
	*/
	//----------------------------------------------------------------------------------
	virtual CGXFeatureControlPointer GetRemoteFeatureControl() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Clear the receiving buffer list for the remote device's event
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void FlushEvent() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Get the current size of receiving buffer list for the remote device's event
	*/
	//----------------------------------------------------------------------------------
	virtual uint32_t GetEventNumInQueue() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Register the device's off-line event
	\param    pUserParam[in]        The user param can be used to distinguish different call back events
	\param    callBackFun[in]       The call back handler pointer; that must be inherited from IDeviceOfflineEventHandler
	\return   The call back handle
	*/
	//----------------------------------------------------------------------------------
	virtual GX_DEVICE_OFFLINE_CALLBACK_HANDLE RegisterDeviceOfflineCallback(IDeviceOfflineEventHandler* pEventHandler, void* pUserParam) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Unregister the device's off-line event
	\param    hCallback[in]        The call back handle
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void UnregisterDeviceOfflineCallback(GX_DEVICE_OFFLINE_CALLBACK_HANDLE hCallback) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief      Export the current feature values of a device to a config file
	\param      [in]strFilePath     The export file path
	\return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void ExportConfigFile(const GxIAPICPP::gxstring& strFilePath) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief      Import a config file to the device
	\param      [in]strFilePath     The import file path
	\return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void ImportConfigFile(const GxIAPICPP::gxstring& strFilePath) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Close the device
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void Close() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Returns a CImageProcessConfigPointer object, 
	which contains a set of set parameters for image processing. e.g. IImageData::ImageProcess 
	\return   A CImageProcessConfigPointer object
	*/
	//----------------------------------------------------------------------------------
	virtual CImageProcessConfigPointer CreateImageProcessConfig() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief      Export the current feature values of a device to a config file
	\param      [in]strFilePath     The export file path
	\return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void ExportConfigFileW(const wchar_t* pchWFilePath) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief      Import a config file to the device
	\param      [in]strFilePath     The import file path
	\return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void ImportConfigFileW(const wchar_t*  pchWFilePath) = 0;
	
	//----------------------------------------------------------------------------------
	/**
	\brief      Get parent interface pointer
	\return      A CGXInterfacePointer object
	*/
	//----------------------------------------------------------------------------------
	virtual CGXInterfacePointer GetParentInterface() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Register the device's reconnect event
	\param    callBackFun[in]       The call back handler pointer; that must be inherited from IDeviceReconnectEventHandler
	\param    pUserParam[in]        The user param can be used to distinguish different call back events
	*/
	//----------------------------------------------------------------------------------
	virtual void RegisterDeviceReconnectCallback(IDeviceReconnectEventHandler* pEventHandler, void* pUserParam) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Unregister the device's reconnect event
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void UnregisterDeviceReconnectCallback() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Register the device's disconnect event
	\param    callBackFun[in]       The call back handler pointer; that must be inherited from IDeviceDisconnectEventHandler
	\param    pUserParam[in]        The user param can be used to distinguish different call back events
	*/
	//----------------------------------------------------------------------------------
	virtual void RegisterDeviceDisconnectCallback(IDeviceDisconnectEventHandler* pEventHandler, void* pUserParam) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Unregister the device's disconnect event
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void UnregisterDeviceDisconnectCallback() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief     Create window operate handle
	\param     emID[in]          Window ID type (currently only property window is supported)
	\param     hParentWnd[in]    This parameter requires passing NULL when called, as the window will pop up for display. 
                                 (This parameter is reserved to support embedded display in the future.)
	                             when passing a valid window handle, it displays as embedded
	\param     hWnd[out]         Returns the handle of the created window
	\return    void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXCreateWnd(GX_WINDOWS_ID emID, GX_WIND_HANDLE hParentWnd, GX_OPERATE_HANDLE* hWnd) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief     Destroy the corresponding window
	\param     hWnd[in]         Window handle to destroy
	\return    void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXDestroyWnd(GX_OPERATE_HANDLE hWnd) = 0;

	//----------------------------------------------------------------------------------
	/**
    \brief      set position of the window.
    \param      [in]hWnd                      The handle of the windows
    \param      [in]nPosX                     X position of the windows
    \param      [in]nPosY                     Y position of the windows
    \param      [in]nWidth                    Width of the windows(To display correctly, it must be greater than or equal to 450.)
    \param      [in]nHeight                   Height of the windows(To ensure proper display, it must be greater than or equal to 600.)
    \return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXSetShowPosition(GX_OPERATE_HANDLE hWnd, int32_t nPosX, int32_t nPosY, int32_t nWidth, int32_t nHeight) = 0;

	//----------------------------------------------------------------------------------
	/**
    \brief      set mode of the window.
    \param      [in]hWnd                      The handle of the windows
    \param		[in]emMode                    The mode of the windows
                                              When set to NON_BLOCK_SHOW_MODE, it will not block subsequent operations.
                                              When set to BLOCK_SHOW_MODE, it will block subsequent operations.
                                              Note: Do not call this if your program is a GUI application (such as Qt, MFC, WinForms, etc.).
    \return     void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXSetShowMode(GX_OPERATE_HANDLE hWnd, GX_WINDOWS_SHOW_MODE emMode) = 0;
	//----------------------------------------------------------------------------------
	/**
	\brief     Set the visibility of the corresponding window
	\param     hWnd[in]         Window handle
	\param     bVisible[in]     true: show, false: hide
	\return    void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXShowWnd(GX_OPERATE_HANDLE hWnd, bool bVisible) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief     Set the title of the corresponding window
	\param     hWnd[in]         Window handle
	\param     strTitle[in]     Window title
	\return    void
	*/
	//----------------------------------------------------------------------------------
	virtual void GXSetWndTitle(GX_OPERATE_HANDLE hWnd, const char* strTitle) = 0;

};

template class GXIAPICPP_API  GXSmartPtr<IGXDevice>;
typedef GXSmartPtr<IGXDevice>            CGXDevicePointer;