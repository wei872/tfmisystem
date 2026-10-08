#pragma once
#include "DxMediaProcDef.h"
#include "GalaxyException.h"
#include "GXIAPIBase.h"
#include "GXSmartPtr.h"

class IGxVideoSaver
{
public:
	virtual ~IGxVideoSaver() {};

	// ---------------------------------------------------------------------------
	/**
	\brief           Add video frames
	\param[in]       pImageBuffer    Image buffer
	*/
	// ---------------------------------------------------------------------------
	virtual void AddFrame(unsigned char* pImageBuf) = 0;

	// ---------------------------------------------------------------------------
	/**
	\brief           Stop recording
	*/
	// ---------------------------------------------------------------------------
	virtual void Close() = 0;
};

template class GXIAPICPP_API                   GXSmartPtr<IGxVideoSaver>;
typedef GXSmartPtr<IGxVideoSaver>            CGxVideoSaverPointer;