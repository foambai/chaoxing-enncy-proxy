# chaoxing-enncy-proxy

超星学习通刷课脚本 × 言溪题库 AI 解答 —— 本地融合代理

让 [Advanced-ChaoXingBot](https://github.com/nothing-7011/Advanced-ChaoXingBot)（即常见的 `chaoxing.exe`）通过它自带的 `TikuAdapter` 题库通道，用 [言溪题库](https://tk.enncy.cn/) 官网后台「AI搜题」生成的**同款参数格式**（`token` + `title` + `options` + `type`）完成搜题，把两边融合起来：

- 官网 dashboard 开启的 **AI 解答（智能模式：先题库后 AI 兜底）** 对脚本完全生效
- 所有查询消耗同一 token，**用量 / 查询记录可以在官网 dashboard 里查到**
- 题库 / AI 返回的答案会被**规范化**成脚本认可的格式（选项原文、正确/错误），解决「从言溪题库获取到的答案类型与题目类型不符，已舍弃」丢答案的问题
- 本地缓存：同一题不重复查询，查不到的题 7 天内不重复消耗次数
- 多 token 轮换：`tokens` 里英文逗号隔开多个，次数不足自动切换
- 可选：大模型视觉兜底，题库查不到的题（含图片题）转发给任意 OpenAI 格式、支持读图的模型

## 背景

刷课脚本走自带 `TikuYanxi` 通道时只发 `question` + `token` 两个参数，实测有两个问题：

1. 带 `<img>` 的图片题会被言溪服务端直接拒绝，**不触发 AI 兜底**；
2. AI 返回的散文式答案过不了脚本内置的答案校验（`cut()` + `check_answer()`），被直接丢弃。

本代理按官网 dashboard 生成的题库配置格式补齐 `title / options / type` 参数后，图片题也能触发言溪的 AI 兜底（实测有效），并负责把答案整理成脚本校验能通过的形式。

## 使用方法

1. 环境要求：Windows + Python 3（无第三方依赖，纯标准库）
2. 把 `config_template.ini` 复制为 `config.ini`，填入你的账号和言溪题库 token（在 [官网后台](https://tk.enncy.cn/user/dashboard) 获取，开启 AI 解答效果更好）
3. 双击 `start_proxy.bat`（或 `python enncy_proxy.py`），保持窗口开启
4. 按平时的方式运行 chaoxing.exe 即可，配置里 `provider=TikuAdapter`、`url=http://127.0.0.1:8765/adapter`

## 工作原理

```
chaoxing.exe ──TikuAdapter HTTP──> enncy_proxy.py ──token+title+options+type──> tk.enncy.cn
                                        │
                                        └──(可选)──> OpenAI 兼容视觉大模型兜底
```

代理收到脚本的题目请求后：先查本地缓存 → 查言溪题库（题库未命中时由言溪侧 AI 兜底）→ 仍未命中且配置了大模型时转发大模型 → 把答案规范化（选项文本匹配、判断题归一化、填空题清洗）→ 按脚本期望的 TikuAdapter 响应格式返回。

## 配置说明

所有配置都在 `config.ini`：

| 配置项 | 说明 |
|---|---|
| `tokens` | 言溪题库 TOKEN，多个用英文逗号隔开，次数不足自动轮换 |
| `submit` / `cover_rate` / `delay` | 刷课脚本身的答题设置，含义同原项目 |
| `llm_endpoint` / `llm_key` / `llm_model` | 可选，OpenAI 格式且支持读图的模型，例如硅基流动的 `Qwen/Qwen2.5-VL-72B-Instruct` |
| `true_list` / `false_list` | 判断题答案归一化的目标词 |

## 致谢

本项目在开发过程中参考、借鉴了以下项目与服务的代码和接口设计，表示感谢：

- [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) —— 原版超星刷课脚本
- [nothing-7011/Advanced-ChaoXingBot](https://github.com/nothing-7011/Advanced-ChaoXingBot) —— 本工具配套的 chaoxing.exe
- [DokiDoki1103/tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter) —— TikuAdapter 题库通道协议
- [言溪题库](https://tk.enncy.cn/) —— 题库与 AI 解答服务

## 免责声明

本项目仅供学习交流使用，请勿用于盈利或商业用途。使用本脚本产生的任何后果由使用者自行承担，与作者无关。请自觉遵守学校与平台的相关规定。
