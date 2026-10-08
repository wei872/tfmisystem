//------------------------------------------------------------------------
/**
\file		GXInterfaceInfo.h
\brief		The Interface enumeration process creates a list of CGXInterfaceInfo objects 
            (GxIAPICPP::gxinterfaceinfo_vector). Each CGXInterfaceInfo object stores the information of a Interface.
            The information is retrieved during the Interface enumeration process (IGXFactory::UpdateDeviceList)
\Date       2022-08-30
\Version    1.0.2208.9301

<p>Copyright (c) 2022-2022  China Daheng Group, Inc. Beijing Image
Vision Technology Branch and all right reserved.</p>
*/
//------------------------------------------------------------------------
#pragma once
#include "IGXInterfaceInfo.h"
#include "GXSmartPtr.h"

#pragma warning(push)
#pragma warning(disable: 4251) // class 'xxx' needs to have dll-interface to be used by clients of class 'yyy'

class IGXInterfaceInfoImpl;
typedef GXSmartPtr<IGXInterfaceInfoImpl> CGXInterfaceInfoImplPointer;

//--------------------------------------------
/**
\brief  Interface info class
*/
//---------------------------------------------
class GXIAPICPP_API CGXInterfaceInfo : public IGXInterfaceInfo
{
public:

    //---------------------------------------------------------
	/**
	\brief Constructor
	*/
	//---------------------------------------------------------
    CGXInterfaceInfo();

    //---------------------------------------------------------
	/**
	\brief Constructor
	*/
	//---------------------------------------------------------
    CGXInterfaceInfo(CGXInterfaceInfoImplPointer& objCGXInterfaceInfoImplPointer);
    
    //---------------------------------------------------------
	/**
	\brief Destructor
	*/
	//---------------------------------------------------------
    virtual ~CGXInterfaceInfo(void);
    
    //---------------------------------------------------------
	/**
	\brief Get interface type
	\return   Interface type
	*/
	//---------------------------------------------------------
    virtual GX_TL_TYPE_LIST GetType() const;

    //---------------------------------------------------------
	/**
	\brief Get interface vendor name
	\return   Interface vendor name
	*/
	//---------------------------------------------------------
    virtual GxIAPICPP::gxstring GetVendorName() const;
    
    //---------------------------------------------------------
	/**
	\brief Get interface model name
	\return   Interface model name
	*/
	//---------------------------------------------------------
    virtual GxIAPICPP::gxstring GetModelName() const;
    
    //---------------------------------------------------------
	/**
	\brief Get interface serial number
	\return   Interface serial number
	*/
	//---------------------------------------------------------
    virtual GxIAPICPP::gxstring GetSerialNumber() const;
    
    //---------------------------------------------------------
	/**
	\brief Get interface display name
	\return   Interface display name
	*/
	//---------------------------------------------------------
    virtual GxIAPICPP::gxstring GetDisplayName() const;
    
    //---------------------------------------------------------
	/**
	\brief Get interface ID (CXP)
	\return   Interface ID
	*/
	//---------------------------------------------------------
    virtual GxIAPICPP::gxstring GetInterfaceID() const;
    
    //---------------------------------------------------------
	/**
	\brief Get interface init flag (CXP)
	\return   Interface init flag
	*/
	//---------------------------------------------------------
    virtual uint32_t GetInitFlag() const;
    
private:
    CGXInterfaceInfoImplPointer        m_objGXInterfaceInfoImplPointer;           ///< Internal use only
};

#pragma warning(pop)
