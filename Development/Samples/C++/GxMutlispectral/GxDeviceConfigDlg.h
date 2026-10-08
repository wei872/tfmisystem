#pragma once
#include "GalaxyIncludes.h"

#include <string>
#include <map>

// CDeviceConfig 对话框

class CGxDeviceConfigDlg : public CDialog
{
	DECLARE_DYNAMIC(CGxDeviceConfigDlg)

public:
	CGxDeviceConfigDlg(CGXFeatureControlPointer objStreamFeatureControlPtr, bool bIsSnap, CWnd* pParent = NULL);   // 标准构造函数
	virtual ~CGxDeviceConfigDlg();

// 对话框数据
#ifdef AFX_DESIGN_TIME
	enum { IDD = IDD_DEVICECONFIG };
#endif

protected:
	virtual void DoDataExchange(CDataExchange* pDX);    // DDX/DDV 支持
		// 生成的消息映射函数
	virtual BOOL OnInitDialog();

	DECLARE_MESSAGE_MAP()
public:
	virtual BOOL PreTranslateMessage(MSG* pMsg);
	afx_msg void OnSelchangeTriggermode();
	afx_msg void OnSelchangeTriggersource();
	afx_msg void OnSelchangeEncodermode();
	afx_msg void OnSelchangeEncoderoutputmode();
	afx_msg void OnChangeEncodervalue();
	afx_msg void OnChangeExposuretime();
	afx_msg void OnChangeGain();
	afx_msg void OnChangeSpectrumroiheight();
	afx_msg void OnChangeSpectrumroioffsety();
	afx_msg void OnSelchangeSpectrumselector();
	afx_msg void OnChangeEncoderdrvider();

private:
	///更新Combobox控件列表信息
	bool __UpdateComBoBoxList(const std::string&  strName, const int& nID);

	///更新int类型控件值
	bool __UpdateIntValue(const std::string& strName, const int& nID, int& nValue);

	///更新float类型控件值
	bool __UpdateFloatValue(const std::string& strName, const int& nID, float& fValue);

private:
	CGXFeatureControlPointer					m_objStreamFeatureControlPtr;
	bool										m_bProgrammaticChange;
	bool										m_bIsSnap;
	std::map<int, CIntFeaturePointer>			m_mapIntRange;
	std::map<int, CFloatFeaturePointer>			m_mapFloatRange;
	std::map<int, int>							m_mapOldValue;
	int											m_nEncoderDrvider;
	int											m_nEncoderValue;
	int											m_nSpectrumROIOffsetY;
	int											m_nSpectrumROIHeight;
	float										m_fExposureTime;
	float										m_fGain;
};
