#pragma once
#include "GalaxyIncludes.h"

#include <vector>

typedef struct IMAGE_INFO {
	void*		pImage;
	uint64_t	nWidth;
	uint64_t	nHeight;
	std::string strBindName;
}IMAGE_INFO;

class CGxImageProcess
{
public:
	/// Í¼Ïñ²ð·ÖÖØ×é
	static bool DivideImage(std::vector<CImageDataPointer>& vecInput,
								const uint64_t& nROIHeight, std::vector<IMAGE_INFO>& vecOutput);

	/// Í¼Ïñ¶ÔÆë
	static bool MatchAndAlign(const std::vector<IMAGE_INFO>& vecInput, const std::vector<uint64_t>& vecGapValue, std::vector<IMAGE_INFO>& vecOutput);

	/// ÏÔÊ¾RGB24Í¼Ïñ
	static void DisplayRGB24Image(CDC* pDC, CRect drawRect, void* pData, const int64_t& width, const int64_t& height);

	///< ±£´æBMPÍ¼Ïñ
	static bool SaveRGB24ToBMP(const CString& filePath, void* pData, const int64_t& width, const int64_t& height);
};

