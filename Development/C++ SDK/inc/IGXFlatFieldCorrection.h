//---------------------------------------------------------------
/**
\file           IGXFlatFieldCorrection.h
\brief        Definition of the IGXFlatFieldCorrection interface
\Date       2025-06-05
\Version    1.1.2506.9051
*/
//---------------------------------------------------------------

#pragma once
#include "GXIAPIBase.h"
#include "GXSmartPtr.h"
#include "IImageData.h"

//--------------------------------------------
/**
\brief    Flat Field Correction class
*/
//---------------------------------------------
class GXIAPICPP_API IGXFlatFieldCorrection
{

public:

    //---------------------------------------------------------
    /**
    \brief Destructor
    */
    //---------------------------------------------------------
    virtual ~IGXFlatFieldCorrection(){};

	//--------------------------------------------------
	/**
	\brief  Set flat field correction frame count
	\param  nFFCFrameCount          [in] flat field correction frame count

	\return emStatus
	*/
	//--------------------------------------------------
    virtual void SetFrameCount(uint16_t nFFCFrameCount) = 0;

	//--------------------------------------------------
	/**
	\brief  Calculate flat field correction coefficients size
	\param  pstFFCParam                  [in] flat field correction parameter
	\return  pnFFCCoefficientsSize        
	*/
	//--------------------------------------------------
	virtual int32_t GetCoefficientsSize(const GX_FLAT_FIELD_CORRECTION_PARAMETER *pstFFCParam) = 0;
    

	//--------------------------------------------------
	/**
	\brief  Calculate flat field correction coefficients
	\param  pstFFCParam             [in] flat field correction parameter
	\param  pFFCCoefficients        [out]flat field correction coefficients
	\param  nFFCCoefficientsSize    [in] flat field correction coefficients size
	*/
	//--------------------------------------------------
	virtual void Calculate(const GX_FLAT_FIELD_CORRECTION_PARAMETER *pstFFCParam, void *pFFCCoefficients, int32_t *pnFFCCoefficientsSize) = 0;

	//--------------------------------------------------
	/**
	\brief  Flat Field Correction Process
	\param  pInputBuffer    	  [in]        Image in
	\param  pOutputBuffer    	  [out]       Image out
	\param  nActualBits           [in]        Image actual cits
	\param  nImgWidth             [in]        Image width
	\param  nImgHeight            [in]        Image height 
	\param  pFFCCoefficients      [in]        Flat field correction coefficients
	\param  pnlength              [in]        Flat field correction coefficients(byte)

	\return emStatus  
	*/
	//--------------------------------------------------
	virtual void FlatFieldCorrection(void *pSrcBuffer, void *pDstBuffer, 
		GX_ACTUAL_BITS emActualBits, uint32_t ui64SrcWidth, 
		uint32_t  ui64SrcHeight, void  *pFFCCoefficients, 
		int *pnFFCCoefficientsSize) = 0;
};

template class GXIAPICPP_API  GXSmartPtr<IGXFlatFieldCorrection>;
typedef GXSmartPtr<IGXFlatFieldCorrection>            CGXFlatFieldCorrectionPointer;

