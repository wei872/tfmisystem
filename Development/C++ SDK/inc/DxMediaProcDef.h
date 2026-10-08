#pragma once
#include <stdint.h>
#include "GXIAPIBase.h"

#if defined(_WIN32)
#ifndef _STDINT_H 
#ifdef _MSC_VER // Microsoft compiler
#if _MSC_VER < 1600
typedef __int8            int8_t;
typedef __int16           int16_t;
typedef __int32           int32_t;
typedef __int64           int64_t;
typedef unsigned __int8   uint8_t;
typedef unsigned __int16  uint16_t;
typedef unsigned __int32  uint32_t;
typedef unsigned __int64  uint64_t;
#else
// In Visual Studio 2010 is stdint.h already included
#include <stdint.h>
#endif
#else
// Not a Microsoft compiler
#include <stdint.h>
#endif
#endif 
#else
// Linux
#include <stdint.h>
#endif

#ifdef _WIN32
#include <Windows.h>
#define DX_DLLIMPORT   __declspec(dllimport)
#define DX_DLLEXPORT   __declspec(dllexport)

#define DX_STDC __stdcall
#define DX_CDEC __cdecl

#if defined(__cplusplus)
#define DX_EXTC extern "C"
#else
#define DX_EXTC
#endif
#else
	// remove the None #define conflicting with GenApi
#undef None
#if __GNUC__>=4
#define DX_DLLIMPORT   __attribute__((visibility("default")))
#define DX_DLLEXPORT   __attribute__((visibility("default")))

#if defined(__i386__)
#define DX_STDC __attribute__((stdcall))
#define DX_CDEC __attribute__((cdecl))
#else
#define DX_STDC
#define DX_CDEC
#endif

#if defined(__cplusplus)
#define DX_EXTC extern "C"
#else
#define DX_EXTC
#endif
#else
#error Unknown compiler
#endif
#endif

#define IMAGE_DIMENSION_MAX          65500  		///<\Chinese 图像维度（宽/高）最大值						\English	Maximum value of image dimensions (width/height)
#define IMAGE_SIZE_MAX				 65500 *65500 	///<\Chinese 图像最大值										\English	Image maximum value
#define IMAGE_DIMENSION_MIN          2  			///<\Chinese 图像维度（宽/高）最小值						\English	Minimum value of image dimensions (width/height)

///插值方法
typedef enum GX_CFA_METHOD
{
	GX_CFA_METHOD_QUICK = 0,    	///<\Chinese 快速															\English	Quickly
	GX_CFA_METHOD_BALANCE = 1,    	///<\Chinese 均衡															\English	Balance
	GX_CFA_METHOD_OPTIMAL = 2,    	///<\Chinese 最优															\English	Optimal
}GX_CFA_METHOD;

///图片格式
typedef enum GX_IMAGE_FORMAT_TYPE
{
	GX_IMAGE_FORMAT_JPEG = 0,	///<JPEG
	GX_IMAGE_FORMAT_PNG,		///<PNG
	GX_IMAGE_FORMAT_TIFF,		///<TIFF
	GX_IMAGE_FORMAT_RAW,		///<RAW
	GX_IMAGE_FORMAT_BMP,		///<BMP
}GX_IMAGE_FORMAT_TYPE;

///视频格式
typedef enum GX_VIDEO_FORMAT_TYPE
{
	GX_VIDEO_FORMAT_H264_AVI = 0, 		///<AVI
	GX_VIDEO_FORMAT_H264_MP4 = 1, 		///<MP4
	GX_VIDEO_FORMAT_ORIGINAL_AVI = 2, 	///RAW AVI
}GX_VIDEO_FORMAT_TYPE;

///保存图像信息
typedef struct GX_SAVE_IMAGE_INFO
{
	unsigned char*          pImageBuffer;   ///< \Chinese 图像buffer															\English	Image buffer
	uint32_t                nWidth;			///< \Chinese 图像宽																\English	Width
	uint32_t                nHeight;		///< \Chinese 图像高																\English	Height
	GX_PIXEL_FORMAT_ENTRY   emSrcFormat;	///< \Chinese 源图像格式															\English	Source image format
	GX_CFA_METHOD           emCfaMethod;    ///< \Chinese 插值方法 0:快速 1:均衡 2:最优											\English	Source image format
	GX_IMAGE_FORMAT_TYPE    emImgFormat;    ///< \Chinese 图片保存格式[bmp/jpg/png/raw/tiff]									\English	Image saving format[bmp/jpg/png/raw/tiff]	
	char*                   pImgPath;       ///< \Chinese 图像保存路径															\English	Image path
	uint32_t                nImgQuality;    ///< \Chinese 编码质量, (0-100]，只对JPEG有效										\English	Coding quality, (0-100]，Only valid for JPEG format

	uint32_t                nReserved[64];
}GX_SAVE_IMAGE_INFO;

///录像参数
typedef struct GX_RECORD_PARAM
{
	GX_PIXEL_FORMAT_ENTRY    emPixelFormat;  ///< \Chinese 像素格式															\English	Pixel format
	GX_VIDEO_FORMAT_TYPE     emVideoFormat;  ///< \Chinese 视频格式[h264_avi/h264_mp4/original_avi]							\English	Video format[h264_avi/h264_mp4/original_avi]
	uint32_t                 nWidth;         ///< \Chinese 图像宽 															\English	Width
	uint32_t                 nHeight;        ///< \Chinese 图像高 															\English	Height
	uint32_t                 nFrameRate;     ///< \Chinese 帧率FPS															\English	Frame rate
	uint32_t                 nBitRate;       ///< \Chinese 码率kbps															\English	Bit rate
	char*                    pPathName;		 ///< \Chinese 保存路径名称														\English	Path
	uint32_t                 nReserved[64];
}GX_RECORD_PARAM;


/* Errors */
  ///< 错误码列表定义
enum DX_MEDIA_ERROR_LIST
{
	DX_ERR_SUCCESS = 0,							///<  \Chinese	成功															\English	Success
	DX_ERR_ERROR = -2001,						///<  \Chinese	不期望发生的未明确指明的内部错误								\English	There is an unspecified
	DX_ERR_NOT_INITIALIZED = -2002,				///<  \Chinese	未初始化														\English	Not initialized
	DX_ERR_FILE_PATH_INVALID = -2003,			///<  \Chinese	文件路径无效													\English	The file path is invalid
	DX_ERR_IMAGE_TOO_LARGE = -2004,				///<  \Chinese	图像太大														\English	The image is too large.
	DX_ERR_INVALID_PARAM = -2005,				///<  \Chinese	无效参数,一般是指针为NULL										\English	Invalid parameter. 
	DX_ERR_FILE_OPEN_FAILED = -2006,			///<  \Chinese	文件打开失败													\English	Failed to open the file
	DX_ERR_COMPRESSION_FAILED = -2007,			///<  \Chinese	存图失败														\English	Failed image saving
	DX_ERR_RESOURCE_EXHAUSTED = - 2009,			///<  \Chinese	资源耗尽														\English	Resources exhausted
	DX_ERR_IMAGE_CONVERT_FAILED = - 2010,		///<  \Chinese	图像转换失败													\English	Image conversion failed
	DX_ERR_VIDEO_SAVE_FAILED = -2011,			///<  \Chinese	视频录制失败													\English	Video recording failed.
	DX_ERR_VIDEO_CONFIG_FAILED = -2012,			///<  \Chinese	视频配置失败													\English	Video configuration failed.
	DX_ERR_NOT_IMPLEMENTED = -2013,				///<  \Chinese	暂不支持														\English	Not implemented.
	DX_ERR_NOT_DEVICE_DETECTED = -2014,			///<  \Chinese	设备未打开														\English	No device was opened.
};

typedef int32_t DX_MEDIA_ERROR;
typedef void* GX_RECORDER_HANDLE;