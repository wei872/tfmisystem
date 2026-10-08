//------------------------------------------------------------------------
/**
\file		IDeviceDisconnectEventHandler.h
\brief		Contains the device off-line event handler base class.
\Date       2016-8-09
\Version    1.1.1608.9091
*/
//------------------------------------------------------------------------

#ifndef GX_DEVICE_DISCONNECT_EVENT_HANDLER_H
#define GX_DEVICE_DISCONNECT_EVENT_HANDLER_H


class IDeviceDisconnectEventHandler
{
  public:
    //---------------------------------------------------------
    /**
    \brief      Destructor
    */
    //---------------------------------------------------------
	virtual ~IDeviceDisconnectEventHandler(void){};

     //---------------------------------------------------------
    /**
    \brief      This method is called when the device was off-line.
    */
    //---------------------------------------------------------
    virtual void DoOnDeviceDisconnectEvent(void* pUserParam) = 0;
};

#endif //GX_DEVICE_DISCONNECT_EVENT_HANDLER_H