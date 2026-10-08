//------------------------------------------------------------------------
/**
\file		IDeviceReconnectEventHandler.h
\brief		Contains the device off-line event handler base class.
\Date       2016-8-09
\Version    1.1.1608.9091
*/
//------------------------------------------------------------------------

#ifndef GX_DEVICE_RECONNECT_EVENT_HANDLER_H
#define GX_DEVICE_RECONNECT_EVENT_HANDLER_H


class IDeviceReconnectEventHandler
{
  public:
    //---------------------------------------------------------
    /**
    \brief      Destructor
    */
    //---------------------------------------------------------
	virtual ~IDeviceReconnectEventHandler(void){};

     //---------------------------------------------------------
    /**
    \brief      This method is called when the device was off-line.
    */
    //---------------------------------------------------------
    virtual void DoOnDeviceReconnectEvent(void* pUserParam) = 0;
};

#endif //GX_DEVICE_RECONNECT_EVENT_HANDLER_H