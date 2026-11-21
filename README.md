# 分切工单与色差仪数据领取

本地联调使用合成布卷、裁剪订单和收发货凭据。资料端为 Python 3.11 标准库服务。

```sh
python3 source_service.py --port 8040
python3 -m unittest discover -s tests -v
```

`GET /` 给当前 clock。演练人员用 `POST /time` 发送整数 clock 前拨。关掉业务客户端不会重置时钟、票号和用量。重新启动此资料端才重新开局。

## 裁床与卷面坐标

`GET /orders`、`/cuts`、`/warehouse` 各返回当前已到 known 轮次的单据。所有坐标单位毫米，布面长度方向 x 和宽度方向 y 的范围均为左闭右开。parent_ends 两个有向端点确定父卷长度范围。按离散毫米解释，正向时子卷段的首个毫米对应父卷区间的低端，倒卷时对应高端减 1。trim 给父卷保留的左右坐标，flip_width 表示从右边界倒数。宽度翻转时子卷 y=0 对应右边界减 1。向后继卷追溯时逐段应用这两向变换。face 表示该段保留的母卷检验面。内部卷可从自己的上游分切单继承检验面，末端母卷分切单记录取样所需的 front/back。装卸单的 piece 是实物标识，后来的坐标复尺不改变它。

candidate_ends 是来源尚未确认的候选，supersedes 指被复尺替换的单据。只在有适用的已公开复尺时收敛候选。成条拼接的不可拆范围在 indivisible。工艺网格以工单原点和 cells_mm 为基准，tracks 是本次实际核查范围。每个相交格在请求的范围内应有各项目至少一份有效观测，位置、面别和按样位的替代关系均须符合；存在不合格的有效观测时不能用同格的合格样点覆盖它。阈值来自工单，不随来源窗口变化。资料充分但超过阈值与资料不足须分别记录。

## 目录与领取

`GET /catalog?roll=M10&face=front&from=1000&until=1800&left=100&right=700` 按范围查询相交窗口。窗口声明 roll、face、span、width、catalog_turn、ready、expires、item、文件类型和内容 SHA-256。ready 轮次含当轮，expires 不含当轮。不同文件可能给同位置的不同项目。JSON 给每个样位和 delta_e 值，CSV 给样位和 light_pass（0/1）。replaces 只替换声明 document 中指定 samples 的本项目观察。窗口没有取到时仍可从目录知道声明的覆盖范围、到期和替代关系。

`POST /ticket` 接受 order（工单编号）、id（窗口编号）、request（稳定领件号）。工单按母卷和本轮计算实际领取名额，429 不发票，425 表示在传。相同领件号只能原样重试，可向 `GET /ticket?request=...` 查既有记录。返回 href 在票的有效期内可取该内容，带整件校验摘要。已经取得的同一窗口可以供其他工单核对，先前成功获得的准确字节不能从变动后的目录凭空补出。

## 装卸凭据

staged 表示备料后仍在仓，sent 表示已离仓，returned 表示按原出库凭据退回实际范围，附实际库位。已送出部分的原坐标解释、旧检测原件和交接号在其 evidence、cuts、destination 中保持。退回原件可按后续工单重新核查。后续发出属于新的交付，尚未回仓的同实物段仍按原交接追踪。

## 裁床客户端（cutter/）

裁床侧的检测收件与放行核对，仅依赖标准库：

- `cutter.geometry`：分切链坐标变换与母卷溯源。有向端点（倒卷时子卷首毫米对应父卷高端减 1）、trim、flip_width 逐段应用；复尺单按 known 轮次替换旧单且只更正指定段；candidate_ends 未收敛前同一段保留多个母卷候选。
- `cutter.store`：已取原件证据库。字节按目录 SHA-256 校验后留存，可落盘、可跨工单复用；复测窗口的 replaces 声明只作废指定样位的本项目观察。
- `cutter.planner`：按工单条带映射领取窗口，expires 临近者优先，不按整卷烧额度；429 只停该工单该母卷本轮，425 下轮再试，领件号稳定可原样重试。
- `cutter.verify`：工艺网格逐格核对。声明覆盖到本范围的窗口未取到记少领原件；取齐但无样位记样点不够；有效观测超工单阈值记明确不符，且不能被同格合格样点覆盖；候选分歧记候选未定。不可拆条带一处缺证整条待确认。
- `cutter.warehouse`：台账三类事实分别核对——备料停/缓、离库按原交接号追回（保留当时放行证据）、退回段按后续工单重核（有资格再发/等补证/不符隔离），未回仓段仍按原交接追踪。
- `cutter.monitor` + `run_scenario.py`：单轮编排与端到端演练。

```sh
python3 run_scenario.py            # 进程内起资料端，拨钟 0..6 演示全流程
python3 run_scenario.py --base http://127.0.0.1:8040 --store evidence-cache
python3 -m unittest discover -s tests -v
```
