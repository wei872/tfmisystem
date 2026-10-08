//------------------------------------------------------------------------
/**
\file		IGXFactory.h
\brief		Definition of IGXFactory class. Initialize library, enumerates and open devices.
\Date       2024-12-03
\Version    1.1.2412.9031
*/
//------------------------------------------------------------------------
#pragma once
#include "GXIAPIBase.h"
#include "GXSmartPtr.h"
#include "IGXDevice.h"
#include "IGXDeviceInfo.h"
#include "IGXInterface.h"
#include "IGXInterfaceInfo.h"
#include "GXStringCPP.h"
#include "GXDeviceInfoVector.h"
#include "IGXImageFormatConvert.h"
#include "IGXImageSpecialFunction.h"
#include "GXInterfaceInfoVector.h"
#include "IGXImageProcess.h"
#include "IGXFlatFieldCorrection.h"
#include "IGXDecompressor.h"
#include "IGxVideoSaver.h"

class GXIAPICPP_API IGXFactory
{
protected:

	///Constructor
	IGXFactory(){}

	///Destructor
	virtual ~IGXFactory(){}

public:
	//----------------------------------------------------------------------------------
	/**
	\brief     Get the static instance object of IGXFactory
	\return    The static instance object of IGXFactory
	*/
	//----------------------------------------------------------------------------------
	static IGXFactory& GetInstance();

	//----------------------------------------------------------------------------------
	/**
	\brief    Initializes the GxIAPICPP runtime system
	You must call Init before calling any other functions.
	When finished you must call Uninit to free up all resources used by GxIAPICPP.
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void Init() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Frees resources allocated by the GxIAPICPP runtime system.
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void Uninit() = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Retrieve a list of available devices. If GigE cameras are connected, this can enumerate all GigE camera devices in the same subnet. 
	\param    nTimeout[in]        Timeout for the waits in ms.
	\param    vectorDeviceInfo[int,out] A list of available devices
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void UpdateDeviceList(uint32_t nTimeout, GxIAPICPP::gxdeviceinfo_vector& vectorDeviceInfo) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Retrieve a list of available devices. If GigE cameras are connected, this can enumerate all GigE camera devices in all subnets. 
	\param    nTimeout[in]        Timeout for the waits in ms.
	\param    vectorDeviceInfo[int,out] A list of available devices
	\return   void
	*/
	//----------------------------------------------------------------------------------
	virtual void UpdateAllDeviceList(uint32_t nTimeout, GxIAPICPP::gxdeviceinfo_vector& vectorDeviceInfo) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Open device by IP address, if present
	\param    strIP[in]        IP address
	\param    emAccessMode[in] The access mode
	\return   A smart pointer object of IGXDevice
	*/
	//----------------------------------------------------------------------------------
	virtual CGXDevicePointer OpenDeviceByIP(const GxIAPICPP::gxstring& strIP, GX_ACCESS_MODE emAccessMode) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Open device by MAC address, if present
	\param    strIP[in]        MAC address
	\param    emAccessMode[in] The access mode
	\return   A smart pointer object of IGXDevice
	*/
	//----------------------------------------------------------------------------------
	virtual CGXDevicePointer OpenDeviceByMAC(const GxIAPICPP::gxstring& strMAC, GX_ACCESS_MODE emAccessMode) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Open device by serials number
	\param    strIP[in]        Serials number
	\param    emAccessMode[in] The access mode
	\return   A smart pointer object of IGXDevice
	*/
	//----------------------------------------------------------------------------------
	virtual CGXDevicePointer OpenDeviceBySN(const GxIAPICPP::gxstring& strSN, GX_ACCESS_MODE emAccessMode) = 0;

	//----------------------------------------------------------------------------------
	/**
	\brief    Open device by user defined name, if present
	\param    strIP[in]        The user defined name
	\param    emAccessMode[in] The access mode
	\return   A smart pointer object of IGXDevice
	*/
	//----------------------------------------------------------------------------------
	virtual CGXDevicePointer OpenDeviceByUserID(const GxIAPICPP::gxstring& strUserID, GX_ACCESS_MODE emAccessMode) = 0;

	// ---------------------------------------------------------------------------
		/**
		\brief       Set Device's Static IP Address
		\param[in]   strDevcieMacAddress   Device's MAC Address
		\param[in]   emIpConfigMode        IP Configure Mode(Static IP/DHCP/LLA)
		\param[in]   strIPAddress          IP Address
		\param[in]   strSubnetMask         Subnet Mask
		\param[in]   strDefaultGateway     Default Gateway
		\param[in]   strUserID             User's Name

		\retrun     void
		*/
       // ---------------------------------------------------------------------------
		virtual void GigEIpConfiguration(const GxIAPICPP::gxstring& strDeviceMacAddress, 
			GX_IP_CONFIGURE_MODE emIpConfigMode,
			const GxIAPICPP::gxstring& strIpAddress,
			const GxIAPICPP::gxstring& strSubnetMask, 
			const GxIAPICPP::gxstring& strDefaultGateway,
			const GxIAPICPP::gxstring& strUserID) = 0;

		// ---------------------------------------------------------------------------
		/**
		\brief       ForceIP
		\param[in]   strDevcieMacAddress  Device's MAC Address
		\param[in]   strIPAddress         Ip Address
		\param[in]   strSubnetMask        Subnet Mask
		\param[in]   strDefaultGateway    Default Gateway

		\retrun      void
		*/
        // ---------------------------------------------------------------------------
		virtual void GigEForceIp(const GxIAPICPP::gxstring& pszDeviceMacAddress, 
			const GxIAPICPP::gxstring& strIpAddress,
			const GxIAPICPP::gxstring& strSubnetMask, 
			const GxIAPICPP::gxstring& strDefaultGateway) = 0;

		// ---------------------------------------------------------------------------
		/**
		\brief       reset/reconnect Device
		\param[in]   pszDevcieMacAddress  Device's MAC Address
		\param[in]   ui32FeatureInfo      reset Mode

		\retrun      void
		*/
		// ---------------------------------------------------------------------------
		virtual void GigEResetDevice(const GxIAPICPP::gxstring& strDeviceMacAddress, GX_RESET_DEVICE_MODE ui32FeatureInfo) = 0;
		
		// ---------------------------------------------------------------------------
		/**
		\brief       create new convert pointer
		\retrun      A smart pointer object of IGXImageFormatConvert
		*/
		// ---------------------------------------------------------------------------
		virtual CGXImageFormatConvertPointer CreateImageFormatConvert() = 0;
		
		// ---------------------------------------------------------------------------
		/**
		\brief       create new specail function pointer
		\retrun      A smart pointer object of IGXImageSpecialFunction
		*/
		// ---------------------------------------------------------------------------
		virtual CGXImageSpecialFunctionPointer CreateImageSpecialFunction() = 0;
		
		//----------------------------------------------------------------------------------
	    /**
	    \brief    Retrieve a list of available devices. If GigE cameras are connected, this can enumerate all GigE camera devices in all subnets. 
	    \param    u32DeviceType[in]   update device type
	    \param    nTimeout[in]        Timeout for the waits in ms.
	    \param    vectorDeviceInfo[int,out] A list of available devices
	    \return   void
	    */
	    //----------------------------------------------------------------------------------
	    virtual void UpdateAllDeviceListEx( uint32_t nTLType, uint32_t nTimeout, GxIAPICPP::gxdeviceinfo_vector& vectorDeviceInfo) = 0;

        //----------------------------------------------------------------------------------
	    /**
	    \brief    Get interface info,call enumeration interface first
	    \param    vectorInterfaceInfo[in]   interface list
	    \return   void
	    */
	    //----------------------------------------------------------------------------------
        virtual void GetAllInterfaceInfo(GxIAPICPP::gxinterfaceinfo_vector& vectorInterfaceInfo) = 0;
        
        //----------------------------------------------------------------------------------
	    /**
	    \brief    Get interface by index,call enumeration interface first
	    \param    nIndex[in]   interface index
	    \return   A smart pointer object of IGXInterface
	    */
	    //----------------------------------------------------------------------------------
        virtual CGXInterfacePointer GetInterface(const uint32_t nIndex) = 0;

		//----------------------------------------------------------------------------------
		/**
		\brief    Create new IGXImageProcess pointer
		\return   IGXImageProcess pointer
		*/
		//----------------------------------------------------------------------------------
		virtual CGXImageProcessPointer CreateImageProcess() = 0;

        //----------------------------------------------------------------------------------
        /**
        \brief      Set whether logs of the specified type can be sent
        \param      [in]ui32LogType   log type: GX_LOG_TYPE_FATAL|GX_LOG_TYPE_ERROR|GX_LOG_TYPE_INFO
                                                GX_LOG_TYPE_OFF: all cannot be sent
        \return
        */
        //----------------------------------------------------------------------------------
        virtual void SetLogType(const uint32_t ui32LogType) = 0;

        //----------------------------------------------------------------------------------
        /**
        \brief      Gets whether logs of a specified type can be sent
        \param      [out]ui32Value  log type value: FATAL|ERROR|WARN|INFO|DEBUG|TRACE
        \return
        */
        //----------------------------------------------------------------------------------
        virtual void GetLogType(uint32_t& ui32Value) = 0;

		//----------------------------------------------------------------------------------
		/**
		\brief      Sending the ActionCommon command allows cameras on the network to execute actions simultaneously.
		\attention  This interface needs to use the device information on the current network card, so before calling it,
		\           you need to call GXUpdateAllDeviceList or other similar interfaces to enumerate the current device list.
		\param      [in]nDeviceKey                   Device key specified by the protocol
		\param      [in]nGroupKey                    The group key specified by the protocol
		\param      [in]nGroupMask                   Protocol-defined group mask
		\param      [in]strBroadcastAddress          The destination IP to send cmd to can be broadcast IP (255.255.255.255), subnet broadcast (192.168.42.255), unicast (192.168.42.42)
		\param      [in]strSpecialAddress            The source IP of the cmd is used to clearly identify the network port from which the command is sent.
		\                                            This can solve the problem of repeated cmd sending in the case of multiple network cards.
		\param      [in]nTimeoutMs			         Optional: Time in milliseconds the call is waiting for acknowledges of the addressed devices.
		\                                            Waiting for acknowledges is stopped if pNumResults have been received.
		\                                            This parameter can be 0 if a check of action command results is not required.
		\param      [in][out]pNumResults             Optional: The number of results in the results array.
		\                                            The value passed should be equal to the expected number of devices that acknowledge the command.
		\                                            Returns the number of received results. This parameter is ignored if timeoutMs is 0.
		\                                            Thus, this parameter can be NULL if timeoutMs is 0.
		\param      [in][out]pResults			     Optional: An array with *pNumResults elements to hold the action command result status.
		\                                            The buffer is filled beginning from the start.
		\                                            Remaining results are not changed if less results are received than result items available.
		\                                            This parameter is ignored if timeoutMs is 0. Thus, this parameter can be NULL if timeoutMs is 0.
		\return   void
		*/
		//----------------------------------------------------------------------------------
		virtual void GigEIssueActionCommand(uint32_t nDeviceKey, uint32_t nGroupKey, uint32_t nGroupMask
			, const GxIAPICPP::gxstring& strBroadcastAddress, const GxIAPICPP::gxstring& strSpecialAddress
			, uint32_t nTimeoutMs, uint32_t* pNumResults, GX_GIGE_ACTION_COMMAND_RESULT* pResults) = 0;

		//----------------------------------------------------------------------------------
		/**
		\brief      Sending the ActionCommon command allows cameras on the network to execute actions simultaneously.
		\attention  This interface needs to use the device information on the current network card, so before calling it,
		\           you need to call GXUpdateAllDeviceList or other similar interfaces to enumerate the current device list.
		\           IP4 addresses must be used. groupMask must not be 0. pNumResult, *pNumResult and pResults must not be 0 if a timeoutMs value other than 0 is passed.
		\param      [in]nDeviceKey                   Device key specified by the protocol
		\param      [in]nGroupKey                    The group key specified by the protocol
		\param      [in]nGroupMask                   Protocol-defined group mask
		\param      [in]nActiontimeNs                The time in nanoseconds to perform the operation.The actual value depends on the master clock used.
		\                                            For example, the master clock value of a set of synchronized camera devices can be obtained by reading the
		\                                            timestamp value GXGetIntValue(hDevice, "TimestampLatch") after latching it from the set of camera devices
		\param      [in]strBroadcastAddress          The destination IP to send cmd to can be broadcast IP (255.255.255.255), subnet broadcast (192.168.42.255), unicast (192.168.42.42)
		\param      [in]strSpecialAddress            Optional: The source IP of the cmd is used to clearly identify the network port from which the command is sent.
		\                                            This can solve the problem of repeated cmd sending in the case of multiple network cards.
		\param      [in]nTimeoutMs                    Optional: Time in milliseconds the call is waiting for acknowledges of the addressed devices.
		\                                            Waiting for acknowledges is stopped if pNumResults have been received.
		\                                            This parameter can be 0 if a check of action command results is not required.
		\param      [in][out]pNumResults             Optional: The number of results in the results array.
		\                                            The value passed should be equal to the expected number of devices that acknowledge the command.
		\                                            Returns the number of received results. This parameter is ignored if timeoutMs is 0.
		\                                            Thus, this parameter can be NULL if timeoutMs is 0.
		\param      [in][out]pResults                Optional: An array with *pNumResults elements to hold the action command result status.
		\                                            The buffer is filled beginning from the start.
		\                                            Remaining results are not changed if less results are received than result items available.
		\                                            This parameter is ignored if timeoutMs is 0. Thus, this parameter can be NULL if timeoutMs is 0.
		\return   void
		*/
		//----------------------------------------------------------------------------------
		virtual void GigEIssueScheduledActionCommand(uint32_t nDeviceKey, uint32_t nGroupKey, uint32_t nGroupMask
			, uint64_t nActiontimeNs, const GxIAPICPP::gxstring& strBroadcastAddress, const GxIAPICPP::gxstring& strSpecialAddress
			, uint32_t nTimeoutMs, uint32_t* pNumResults, GX_GIGE_ACTION_COMMAND_RESULT* pResults) = 0;

		// ---------------------------------------------------------------------------
		/**
		\brief       create new FlatFieldCorrection
		\retrun      A smart pointer object of IGXFlatFieldCorrection
		*/
		// ---------------------------------------------------------------------------
		virtual CGXFlatFieldCorrectionPointer CreateFlatFieldCorrection() = 0;

        //----------------------------------------------------------------------------------
        /**
        \brief    Create new IGXDecompressor pointer
        \return   IGXDecompressor pointer
        */
        //----------------------------------------------------------------------------------
        virtual CGXDecompressorPointer CreateDecompressor() = 0;

		// ---------------------------------------------------------------------------
		/**
		\brief           Save image
		\param[in]       stSaveImageInfo    Image information
		\retrun          void
		*/
		// ---------------------------------------------------------------------------
		virtual void SaveImage(GX_SAVE_IMAGE_INFO * stSaveImageInfo) = 0;

		// ---------------------------------------------------------------------------
		/**
		\brief           Create video object
		\param[in]       stRecordParam    Video information
		\retrun          Video object
		*/
		// ---------------------------------------------------------------------------
		virtual CGxVideoSaverPointer CreateVideoSaver(GX_RECORD_PARAM * stRecordParam) = 0;
private:
	IGXFactory& operator=(const IGXFactory&);
	IGXFactory(const IGXFactory&);
};

