// SimpleService —— Python 侧对 Java/前端暴露的入向控制接口。
//
// 这份 IDL 是按仓库里已生成的 lib/camera/thrift/SimpleService.py 反推补写的：
// 仓库原本只有生成结果、没有 IDL，导致 Python 侧加方法时 Java 侧无从同步。
// 改接口请**先改这里，再两边重新生成**，不要只改一边。
//
//   thrift --gen py  simple_service.thrift
//   thrift --gen java simple_service.thrift
//
// 字段编号一旦发布就不要再改或复用 —— Thrift 靠编号而不是名字对齐。

service SimpleService {
    bool   getIsStop(),
    bool   setIsStop(1: bool value, 2: string status),
    string ping(),

    // ---------- 【四】正常图片定时保存 ----------
    // 开启一个窗口：这段时间内定时保存"本帧没检出任何缺陷"的图，
    // 每台相机每个窗口最多 normal_image_save.max_per_camera 张（默认 10）。
    //
    // start 支持三种写法：
    //   ""            -> 立即开始
    //   epoch 秒字符串 -> 绝对时刻
    //   "HH:MM"/"HH:MM:SS" -> 今天的该时刻，已过则顺延到明天
    // duration_sec <= 0 视为无效，返回 false（不开窗口）。
    bool setNormalSaveWindow(1: string start, 2: double duration_sec),

    // 提前结束窗口。
    bool stopNormalSave(),

    // 查进度。返回 JSON 字符串，字段见
    // AnomalyDetection_Tool/services/normal_image_saver.py 的 get_status()。
    string getNormalSaveStatus(),
}
