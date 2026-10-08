//---------------------------------------------------------------
/**
\file         IGxDecompressor.h
\brief        Definition of the IGXDecompressor interface
\Date         2025-09-16
\Version      1.1.2509.9161
<p>Copyright (c) 2011-2025 China Daheng Group, Inc. Beijing Image
Vision Technology Branch and all right reserved.</p>
*/
//---------------------------------------------------------------

#pragma once
#include "GXIAPIBase.h"
#include "GXSmartPtr.h"


//--------------------------------------------
/**
\brief    Image Decompreesor class
*/
//---------------------------------------------
class GXIAPICPP_API IGXDecompressor
{

public:

    //---------------------------------------------------------
    /**
    \brief Destructor
    */
    //---------------------------------------------------------
    virtual ~IGXDecompressor() {};


    //--------------------------------------------------
    /**
    \brief  decompression image
    \param  pCompressionBuffer   		[in]     compression image buffer pointer
    \param  nCompressionSize			[in]     compression image buffer size
    \param  pDecompressionBuffer   		[out]    decompression image buffer pointer
    \param  nDecompressionSize			[in|out] decompression image buffer size
    \param  pCompressionBuffer   		[in]     image pixel format
    \param  ui64ImgWidth   		        [in]     image width
    \param  ui64ImgHeight   		    [in]     image height
    \return void
    */
    //--------------------------------------------------
    virtual void Decompression(void* pCompressionBuffer, uint64_t nCompressionSize, void* pDecompressionBuffer, uint64_t* nDecompressionSize,
        GX_PIXEL_FORMAT_ENTRY emPixelFormat, uint64_t ui64ImgWidth, uint64_t ui64ImgHeight, int32_t i32CompressionMedth) = 0;

    //The extended adjustment parameters are added below and cannot be added to existing functions
    //...
};

template class GXIAPICPP_API                   GXSmartPtr<IGXDecompressor>;
typedef GXSmartPtr<IGXDecompressor>            CGXDecompressorPointer;

