/* 视图：枯水期供水保障 —— 水库管理员申请 / 调度员审核 / 乡镇确认优先级并配水 / 完成扣减库容回写预警 */
window.SupplyView = {
  name: "SupplyView",
  data() {
    return {
      reservoirs: [],
      plans: [],
      supplies: [],
      current: null,              // 当前选中计划单详情
      role: "reservoir_manager",  // 当前扮演角色
      operatorNames: { reservoir_manager: "周库管", dispatcher: "张调度",
                       township: "吴乡镇", supply_manager: "陈物资" },
      loading: false,
      actionNote: "",
      // 申请表单（弹窗）
      applyOpen: false,
      applyBusy: false,
      applyForm: { reservoir_id: null, title: "", period: "",
                   items: [{ township: "", demand: 10 }] },
      // 配水优先级 / 完成实供编辑暂存 {item_id: value}
      prioEdit: {},
      actualEdit: {},
      // 应急物资追加表单
      emForm: { supply_id: null, quantity: 10, reason: "" },
    };
  },
  computed: {
    roles() {
      return [
        { id: "reservoir_manager", name: "水库管理员" },
        { id: "dispatcher", name: "调度员" },
        { id: "township", name: "乡镇联络员" },
        { id: "supply_manager", name: "物资管理员" },
      ];
    },
    roleText() {
      return { reservoir_manager: "水库管理员", dispatcher: "调度员",
               township: "乡镇联络员", supply_manager: "物资管理员" };
    },
    steps() {
      return [
        { key: "applied", name: "申请", role: "水库管理员", actor: "applied_by", at: "applied_at" },
        { key: "approved", name: "审核", role: "调度员", actor: "reviewed_by", at: "reviewed_at" },
        { key: "executing", name: "配水", role: "乡镇联络员", actor: "executed_by", at: "executed_at" },
        { key: "completed", name: "完成", role: "乡镇联络员", actor: "completed_by", at: "completed_at" },
      ];
    },
    resMap() {
      const m = {};
      this.reservoirs.forEach(r => { m[r.id] = r; });
      return m;
    },
    openPlans() {
      return this.plans.filter(p => p.status !== "completed");
    },
    donePlans() {
      return this.plans.filter(p => p.status === "completed");
    },
    snap() { return this.current && this.current.snapshot ? this.current.snapshot : {}; },
    writeback() { return this.snap.writeback || null; },
    canReview() { return this.current && this.current.status === "applied" && this.role === "dispatcher"; },
    canExecute() { return this.current && this.current.status === "approved" && this.role === "township"; },
    canComplete() { return this.current && this.current.status === "executing" && this.role === "township"; },
    canEmergency() {
      return this.current && this.current.status === "completed"
        && this.current.shortage > 0 && this.role === "supply_manager";
    },
  },
  methods: {
    statusBadge(st) {
      return { applied: "orange", approved: "blue", executing: "yellow", completed: "green" }[st] || "gray";
    },
    stepIndex(status) {
      return { applied: 0, approved: 1, executing: 2, completed: 3 }[status] ?? -1;
    },
    stepReached(key, status) {
      const order = ["applied", "approved", "executing", "completed"];
      return order.indexOf(status) >= order.indexOf(key);
    },
    itemBadge(st) {
      return { pending: "orange", supplied: "green", short: "red" }[st] || "gray";
    },
    availableOf(r) {
      // 可供水量 = 当前库容 − 死库容（库容曲线起点）
      const curve = (r.storage_curve || []).slice().sort((a, b) => a[0] - b[0]);
      const dead = curve.length ? curve[0][1] : 0;
      return Math.max((r.current_storage || 0) - dead, 0);
    },
    async load() {
      this.loading = true;
      try {
        const [reservoirs, plans, supplies] = await Promise.all([
          API.reservoirs(), API.supplyPlans(), API.supplies()]);
        this.reservoirs = reservoirs;
        this.plans = plans;
        this.supplies = supplies;
        if (this.current) {
          this.current = plans.find(p => p.id === this.current.id) || null;
          this.resetEdits();
        }
      } catch (e) {
        window.app.showToast("加载供水保障数据失败：" + e.message);
      } finally {
        this.loading = false;
      }
    },
    selectPlan(p) {
      this.current = p;
      this.actionNote = "";
      this.resetEdits();
    },
    backToList() { this.current = null; },
    resetEdits() {
      // 配水优先级默认沿用当前值；实供默认按计划分配足额
      this.prioEdit = {};
      this.actualEdit = {};
      (this.current ? this.current.items : []).forEach(it => {
        this.prioEdit[it.id] = it.priority;
        this.actualEdit[it.id] = it.allocated;
      });
      const firstSupply = this.supplies.find(s => s.available > 0) || this.supplies[0];
      this.emForm = { supply_id: firstSupply ? firstSupply.id : null,
                      quantity: 10, reason: "" };
    },
    remarks() {
      return (this.current && this.current.remark ? this.current.remark.split("\n") : []);
    },
    // ---------------- 申请 ----------------
    openApply(res) {
      this.applyForm = {
        reservoir_id: res.id,
        title: `${res.name}枯水期供水保障计划`,
        period: "",
        items: [{ township: "", demand: 10 }],
      };
      this.applyOpen = true;
    },
    closeApply() { this.applyOpen = false; this.applyBusy = false; },
    addItemRow() { this.applyForm.items.push({ township: "", demand: 10 }); },
    removeItemRow(i) {
      if (this.applyForm.items.length > 1) this.applyForm.items.splice(i, 1);
    },
    async submitApply() {
      const f = this.applyForm;
      const items = f.items.filter(it => (it.township || "").trim() && Number(it.demand) > 0);
      if (!items.length) { window.app.showToast("请至少填写一个乡镇的需水明细"); return; }
      this.applyBusy = true;
      try {
        const p = await API.applySupplyPlan({
          reservoir_id: f.reservoir_id, title: f.title.trim(), period: f.period.trim(),
          items: items.map(it => ({ township: it.township.trim(), demand: Number(it.demand) })),
          operator: this.operatorNames.reservoir_manager, role: "reservoir_manager",
        });
        window.app.showToast(`供水计划 #${p.id} 已提交，待调度员审核`);
        this.applyOpen = false;
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("提交失败：" + e.message); }
      finally { this.applyBusy = false; }
    },
    // ---------------- 审核 / 配水 / 完成 ----------------
    async act(action) {
      const id = this.current.id;
      const body = { operator: this.operatorNames[this.role], role: this.role };
      const note = this.actionNote.trim();
      let api, verb;
      if (action === "review") {
        api = API.reviewSupplyPlan; verb = "审核通过"; body.opinion = note;
      } else if (action === "execute") {
        api = API.executeSupplyPlan; verb = "启动配水";
        body.priorities = {};
        this.current.items.forEach(it => {
          const v = Number(this.prioEdit[it.id]);
          if (v >= 1) body.priorities[it.id] = v;
        });
        body.note = note;
      } else {
        api = API.completeSupplyPlan; verb = "确认完成";
        body.actuals = {};
        this.current.items.forEach(it => {
          const v = Number(this.actualEdit[it.id]);
          if (!isNaN(v)) body.actuals[it.id] = v;
        });
        body.summary = note;
      }
      try {
        const p = await api(id, body);
        const extra = action === "complete"
          ? `：实供 ${p.actual_supply} 万m³，库容扣减至 ${p.snapshot.writeback.final_storage} 万m³`
            + (p.shortage > 0 ? `，异常欠供 ${p.shortage} 万m³` : "")
          : "";
        window.app.showToast(`供水计划 #${id} ${verb}${extra}`);
        this.actionNote = "";
        await this.load();
        this.selectPlan(this.plans.find(x => x.id === id) || p);
      } catch (e) { window.app.showToast(`${verb}失败：` + e.message); }
    },
    // ---------------- 应急物资 ----------------
    async submitEmergency() {
      const f = this.emForm;
      if (!f.supply_id || !(f.quantity > 0)) {
        window.app.showToast("请选择物资并填写追加数量");
        return;
      }
      try {
        const p = await API.appendSupplyEmergency(this.current.id, {
          supply_id: f.supply_id, quantity: Number(f.quantity),
          reason: f.reason.trim(),
          operator: this.operatorNames.supply_manager, role: "supply_manager",
        });
        window.app.showToast("应急物资已追加并出库");
        await this.load();
        this.selectPlan(this.plans.find(x => x.id === p.id) || p);
      } catch (e) { window.app.showToast("追加失败：" + e.message); }
    },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">枯水期供水保障
      <span class="sub">申请 · 审核 · 乡镇优先级配水 · 完成扣减库容并回写预警</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 角色切换 -->
    <div class="panel">
      <div class="panel-body" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:#7d95b4">当前值守角色</span>
        <button v-for="r in roles" :key="r.id" class="btn sm"
                :class="{primary: role===r.id}" @click="role=r.id">{{ r.name }}</button>
        <div class="field" style="margin-left:auto">
          <label>操作人（电子签名）</label>
          <input v-model="operatorNames[role]" class="role-input" style="min-width:160px"/>
        </div>
      </div>
    </div>

    <!-- 列表视图 -->
    <template v-if="!current">
      <div class="panel">
        <div class="panel-head">水库可供水量 <span class="tag">同一水库仅一张开口计划单 · 重复申请幂等归并</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>水库</th><th>当前水位</th><th>当前库容</th><th>可供水量</th><th>开口计划</th><th></th></tr></thead>
            <tbody>
              <tr v-for="r in reservoirs" :key="r.id">
                <td>{{ r.name }}</td>
                <td class="num mono">{{ fmt.num(r.current_level, 2) }} m</td>
                <td class="num mono">{{ fmt.num(r.current_storage, 1) }} 万m³</td>
                <td class="num mono" style="color:var(--water)">{{ fmt.num(availableOf(r), 1) }} 万m³</td>
                <td>
                  <span v-if="openPlans.find(p => p.reservoir_id === r.id)" class="badge"
                        :class="statusBadge(openPlans.find(p => p.reservoir_id === r.id).status)">
                    #{{ openPlans.find(p => p.reservoir_id === r.id).id }}
                    {{ openPlans.find(p => p.reservoir_id === r.id).status_text }}
                  </span>
                  <span v-else class="badge gray">无</span>
                </td>
                <td style="text-align:right">
                  <button class="btn sm primary" :disabled="role!=='reservoir_manager'"
                          @click="openApply(r)">发起供水计划</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel" v-if="plans.length">
        <div class="panel-head">供水计划台账 <span class="tag">{{ plans.length }} 单</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>单号</th><th>标题</th><th>水库</th><th>供水期</th><th>计划/分配/实供</th><th>欠供</th><th>状态</th><th>申请时间</th><th></th></tr></thead>
            <tbody>
              <tr v-for="p in plans" :key="p.id" style="cursor:pointer" @click="selectPlan(p)">
                <td class="mono">#{{ p.id }}</td>
                <td>{{ p.title }}</td>
                <td>{{ p.reservoir_name }}</td>
                <td style="font-size:12px">{{ p.period || '—' }}</td>
                <td class="num mono">{{ fmt.num(p.planned_supply,0) }} / {{ fmt.num(p.allocated_supply,0) }} / {{ fmt.num(p.actual_supply,0) }}</td>
                <td>
                  <span v-if="p.shortage > 0" class="badge red">欠供 {{ fmt.num(p.shortage,1) }}</span>
                  <span v-else class="badge gray">—</span>
                </td>
                <td><span class="badge" :class="statusBadge(p.status)">{{ p.status_text }}</span></td>
                <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(p.applied_at) }}</td>
                <td style="text-align:right"><button class="btn sm" @click.stop="selectPlan(p)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="panel" v-else>
        <div class="panel-body" style="text-align:center;color:#7d95b4;padding:26px">
          暂无供水计划：枯水期由水库管理员围绕水库发起供水保障计划
        </div>
      </div>
    </template>

    <!-- 详情视图 -->
    <template v-else>
      <div class="panel">
        <div class="panel-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <button class="btn sm" @click="backToList">← 返回</button>
            <div style="font-size:16px;font-weight:700">供水计划 #{{ current.id }} · {{ current.title }}</div>
            <span class="badge" :class="statusBadge(current.status)">{{ current.status_text }}</span>
            <span class="badge red" v-if="current.shortage > 0">异常欠供 {{ fmt.num(current.shortage,1) }} 万m³</span>
            <span style="margin-left:auto;font-size:12px;color:#7d95b4">
              {{ current.reservoir_name }} · 供水期 {{ current.period || '未填写' }}
            </span>
          </div>
        </div>
      </div>

      <!-- 四态流转 -->
      <div class="panel">
        <div class="panel-head">协同流转 <span class="tag">申请 → 审核 → 配水 → 完成</span></div>
        <div class="panel-body">
          <div class="flow-steps">
            <template v-for="(s, i) in steps" :key="s.key">
              <div class="flow-step"
                   :class="{done: stepReached(s.key, current.status), active: current.status===s.key}">
                <div class="dot">{{ i + 1 }}</div>
                <div class="meta">
                  <div class="name">{{ s.name }}</div>
                  <div class="role">{{ s.role }}</div>
                  <div class="who" v-if="current[s.actor]">{{ current[s.actor] }} · {{ fmt.time(current[s.at]) }}</div>
                </div>
              </div>
              <div class="flow-arrow" v-if="i < steps.length - 1">→</div>
            </template>
          </div>

          <div style="margin-top:16px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:260px" v-if="canReview || canExecute || canComplete">
              <label>{{ canReview ? '审核意见' : canExecute ? '配水说明' : '完成小结' }}（可选）</label>
              <input v-model="actionNote"
                     :placeholder="canReview ? '如：同意按计划供水，注意蓄水保供' : canExecute ? '如：优先保障居民生活用水' : '记录供水完成情况…'"/>
            </div>
            <button v-if="canReview" class="btn primary" @click="act('review')">✓ 审核通过</button>
            <button v-if="canExecute" class="btn primary" @click="act('execute')">▶ 确认优先级并启动配水</button>
            <button v-if="canComplete" class="btn primary" @click="act('complete')">✔ 确认完成并扣减库容</button>
            <span v-if="current.status!=='completed' && !(canReview || canExecute || canComplete)"
                  style="font-size:12.5px;color:#7d95b4">
              当前角色（{{ roleText[role] }}）本环节无待办，可切换角色继续流转
            </span>
            <span v-if="current.status==='completed'" class="badge green">供水已闭环</span>
          </div>
        </div>
      </div>

      <!-- 水量总览 -->
      <div class="stats">
        <div class="stat blue"><div class="k">计划供水</div><div class="v">{{ fmt.num(current.planned_supply,1) }}<small>万m³</small></div></div>
        <div class="stat"><div class="k">分配供水</div><div class="v">{{ fmt.num(current.allocated_supply,1) }}<small>万m³</small></div></div>
        <div class="stat green"><div class="k">实际供水</div><div class="v">{{ fmt.num(current.actual_supply,1) }}<small>万m³</small></div></div>
        <div class="stat" :class="current.shortage > 0 ? 'red' : 'green'">
          <div class="k">欠供水量</div>
          <div class="v" :style="current.shortage > 0 ? 'color:var(--red-lv)' : ''">{{ fmt.num(current.shortage,1) }}<small>万m³</small></div>
        </div>
        <div class="stat amber" v-if="writeback">
          <div class="k">回写后库容</div>
          <div class="v">{{ fmt.num(writeback.final_storage,1) }}<small>万m³ · {{ fmt.num(writeback.final_level,2) }}m</small></div>
        </div>
      </div>

      <!-- 乡镇配水明细 -->
      <div class="panel">
        <div class="panel-head">乡镇配水明细
          <span class="tag">{{ current.status==='approved' ? '乡镇确认优先级（1 最高）' : current.status==='executing' ? '填报各乡镇实际供水量' : '申请 → 审核 → 配水 → 完成' }}</span>
        </div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>乡镇</th><th>申报需水</th><th>优先级</th><th>计划分配</th><th>实际供水</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="it in current.items" :key="it.id">
                <td>{{ it.township }}</td>
                <td class="num mono">{{ fmt.num(it.demand,1) }} 万m³</td>
                <td>
                  <input v-if="canExecute" type="number" min="1" style="width:64px"
                         v-model.number="prioEdit[it.id]"/>
                  <span v-else class="badge gray">P{{ it.priority }}</span>
                </td>
                <td class="num mono">{{ fmt.num(it.allocated,1) }} 万m³</td>
                <td>
                  <input v-if="canComplete" type="number" min="0" :max="it.allocated" step="0.1"
                         style="width:90px" v-model.number="actualEdit[it.id]"/>
                  <span v-else class="num mono">{{ current.status==='completed' ? fmt.num(it.actual,1) + ' 万m³' : '—' }}</span>
                </td>
                <td>
                  <span v-if="current.status==='completed'" class="badge" :class="itemBadge(it.status)">{{ it.status_text }}</span>
                  <span v-else class="badge gray">待供水</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="row">
        <!-- 回写情况 -->
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">库容扣减与预警回写 <span class="tag">完成时联动</span></div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:12px;font-size:13px">
              <div class="writeback-item">
                <span class="badge blue">申请时</span>
                <span>水位 {{ fmt.num(snap.level,2) }} m · 库容 {{ fmt.num(snap.storage,1) }} 万m³
                  · 可供 {{ fmt.num(snap.available,1) }} 万m³</span>
              </div>
              <div class="writeback-item">
                <span class="badge" :class="writeback ? 'green' : 'gray'">{{ writeback ? '已回写' : '待完成' }}</span>
                <span v-if="writeback">库容扣减至 {{ fmt.num(writeback.final_storage,1) }} 万m³
                  · 水位 {{ fmt.num(writeback.final_level,2) }} m（实时监测/联合调度同步）</span>
                <span v-else>完成后按实际供水扣减库容并反算水位</span>
              </div>
              <div class="writeback-item">
                <span class="badge" :class="writeback ? 'green' : 'gray'">{{ writeback ? '已回写' : '待完成' }}</span>
                <span>枯水预警写入预警台账（run_id 独立台账，预报重跑不清除）</span>
              </div>
              <div class="writeback-item">
                <span class="badge" :class="current.shortage > 0 ? 'red' : 'gray'">
                  {{ current.shortage > 0 ? '异常欠供' : '—' }}
                </span>
                <span>欠供时由物资管理员追加应急物资（库存增量出库，兼容防汛处置预占）</span>
              </div>
            </div>
          </div>

          <div class="panel" v-if="remarks().length">
            <div class="panel-head">处置记录</div>
            <div class="panel-body" style="font-size:12.5px;color:#b9cbe2;line-height:1.9">
              <div v-for="(line, i) in remarks()" :key="i">{{ line }}</div>
            </div>
          </div>
        </div>

        <!-- 应急物资 -->
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">异常欠供 · 应急物资追加 <span class="tag">物资管理员 · 增量出库</span></div>
            <div class="panel-body">
              <div v-if="canEmergency" class="res-form">
                <select v-model.number="emForm.supply_id">
                  <option v-for="s in supplies" :key="s.id" :value="s.id" :disabled="s.available<=0">
                    {{ s.name }} · 可分 {{ s.available }}{{ s.unit }}
                  </option>
                </select>
                <input type="number" min="1" v-model.number="emForm.quantity" placeholder="数量"/>
                <input v-model="emForm.reason" placeholder="追加事由（欠供乡镇/水量）"/>
                <button class="btn sm primary" @click="submitEmergency">追加并出库</button>
              </div>
              <div v-if="current.shortage > 0 && role!=='supply_manager'"
                   style="font-size:11.5px;color:#ffb061;margin:4px 0">
                存在异常欠供 {{ fmt.num(current.shortage,1) }} 万m³，请切换「物资管理员」追加应急物资
              </div>
              <table class="grid" style="margin-top:10px">
                <thead><tr><th>物资</th><th>数量</th><th>事由</th><th>追加人</th><th>时间</th></tr></thead>
                <tbody>
                  <tr v-for="em in current.emergencies" :key="em.id">
                    <td>{{ em.supply_name }}</td>
                    <td class="num">{{ em.quantity }} {{ em.unit }}</td>
                    <td style="font-size:12px">{{ em.reason }}</td>
                    <td style="font-size:11.5px;color:#7d95b4">{{ em.created_by }}</td>
                    <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(em.created_at) }}</td>
                  </tr>
                  <tr v-if="!current.emergencies.length">
                    <td colspan="5" style="color:#7d95b4;text-align:center;padding:16px">
                      {{ current.shortage > 0 ? '尚未追加应急物资' : '无欠供，无需追加应急物资' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">应急物资台账 <span class="tag">库存 − 防汛预占 = 可追加</span></div>
            <div class="panel-body" style="max-height:220px;overflow-y:auto">
              <div v-for="s in supplies" :key="'p'+s.id" class="res-pool">
                <span>{{ s.name }}</span>
                <span>
                  <span class="badge" v-if="s.low" style="margin-right:6px">低于安全库存</span>
                  <span class="num mono">{{ s.available }}/{{ s.stock }} {{ s.unit }}</span>
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 发起供水计划弹窗 -->
    <div v-if="applyOpen" class="modal-mask" @click.self="closeApply">
      <div class="modal">
        <div class="modal-head">
          发起枯水期供水计划
          <button class="modal-close" @click="closeApply">×</button>
        </div>
        <div class="modal-body">
          <p style="font-size:12.5px;color:#b9cbe2;line-height:1.9;margin:0 0 12px">
            水库管理员围绕 <b>{{ resMap[applyForm.reservoir_id] ? resMap[applyForm.reservoir_id].name : '' }}</b>
            提交枯水期供水计划：计划供水量不得超过水库可供水量（当前库容 − 死库容）；
            审核通过后由乡镇确认优先级并执行配水，完成后按实际供水扣减库容并回写枯水预警。
          </p>
          <div class="field">
            <label>计划标题</label>
            <input v-model="applyForm.title"/>
          </div>
          <div class="field" style="margin-top:10px">
            <label>供水期（如 2026-01 ~ 2026-03 枯水期）</label>
            <input v-model="applyForm.period" placeholder="选填"/>
          </div>
          <div class="field" style="margin-top:10px">
            <label>乡镇需水明细（万m³）</label>
            <div v-for="(it, i) in applyForm.items" :key="i"
                 style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
              <input v-model="it.township" placeholder="乡镇名称" style="flex:2"/>
              <input type="number" min="1" step="1" v-model.number="it.demand"
                     placeholder="需水量" style="flex:1"/>
              <button class="btn xs danger" :disabled="applyForm.items.length<=1"
                      @click="removeItemRow(i)">删除</button>
            </div>
            <button class="btn sm" style="align-self:flex-start" @click="addItemRow">+ 添加乡镇</button>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="closeApply">取消</button>
          <button class="btn primary" :disabled="applyBusy" @click="submitApply">
            {{ applyBusy ? "提交中…" : "提交计划（待调度员审核）" }}
          </button>
        </div>
      </div>
    </div>
  </div>`,
};
