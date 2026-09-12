from pathlib import Path
import re, unittest

ROOT=Path(__file__).resolve().parents[1]
class FrontendContractTests(unittest.TestCase):
    def test_build_marker_and_cache_versions_match(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8'); sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        self.assertIn('Date.parse(document.lastModified)', html)
        self.assertIn('sw.js?v=${encodeURIComponent(version)}', html)
        self.assertIn("searchParams.get('v')", sw)
        self.assertIn('const CACHE = `dash-${VERSION}`', sw)
    def test_shadow_status_hook_is_present(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="shadowStatus"', html)
        self.assertIn("import('./frontend-modules/build-status.js')", html)
        module=(ROOT/'frontend-modules'/'build-status.js').read_text(encoding='utf-8')
        self.assertIn("apiURL('/api/shadow/status?check=1')", (ROOT/'frontend-modules'/'data-client.js').read_text(encoding='utf-8'))
        self.assertIn('refreshBuildStatus', module)
        self.assertIn('差异 ${drift}', module)
        self.assertIn('aria-label', module)
        self.assertIn('shadowDetailPanel', html)
        self.assertIn('initBuildStatus', module)
        self.assertIn('shadow_details', module)
        self.assertIn('reconcileShadow', module)
    def test_api_wrapper_handles_non_json_errors(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("Accept:'application/json'", html)
        self.assertIn('响应不是有效 JSON', html)
        self.assertIn('if(!r.ok&&body.success!==false)', html)

    def test_api_wrapper_attaches_mutation_keys_to_write_routes(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('const MUTATION_ROUTES=new Set', html)
        self.assertIn('function mutationKey(route,d)', html)
        self.assertIn('clientMutationId:mutationKey(route,d)', html)
        self.assertIn('JSON.stringify(payload)', html)
    def test_heavy_noncritical_scripts_are_deferred(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('<script src="chart.umd.min.js" defer></script>', html)
        self.assertIn('<script src="quotes.js" defer></script>', html)

    def test_first_frame_css_prevents_unstyled_svg_and_stargaze_flash(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        critical=html.index('<style id="critical-first-frame">')
        first_external_script=html.index('<script src="chart.umd.min.js" defer></script>')
        first_visible_icon=html.index('<svg class="ui-ic">')
        self.assertLess(critical, first_external_script)
        self.assertLess(critical, first_visible_icon)
        self.assertIn('svg.ui-ic{width:22px;height:22px;', html[critical:first_external_script])
        self.assertIn('body:not(.stargazing) #sky-canvas', html[critical:first_external_script])
        self.assertIn('body:not(.stargazing) #sg-moon', html[critical:first_external_script])
    def test_status_badge_is_announced_accessibly(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="shadowStatus" role="status" aria-live="polite"', html)

    def test_domain_sync_status_is_visible_and_centralized(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="sync-domain-status"', html)
        self.assertIn('const SYNC_DOMAINS=', html)
        self.assertIn('function syncDomainForRoute', html)
        self.assertIn("dash-write-result", html)
        module=(ROOT/'frontend-modules'/'sync-ui.js').read_text(encoding='utf-8')
        self.assertIn('export function createSyncUI', module)
        self.assertIn("localStorage.setItem(EVENT_KEY", module)
        self.assertIn('影子数据对账', module)
        self.assertIn('window.__syncUIReady', (ROOT/'frontend-modules'/'build-status.js').read_text(encoding='utf-8'))
        self.assertIn("source:'TickTick'", html)
        self.assertIn("if(domain&&m==='POST')", html)
        self.assertIn("setSyncDomain('core','ok','启动数据已就绪')", html)
        self.assertNotIn("setSyncDomain(domain,m==='POST'?'pending':'pending')", html)
        self.assertIn("const syncSamples = []", (ROOT/'test_e2e.mjs').read_text(encoding='utf-8'))

    def test_foldable_touch_layout_has_safe_targets_and_fixed_nav(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('@media(max-width:600px)', html)
        self.assertIn('env(safe-area-inset-bottom', html)
        self.assertIn('.mn-inner{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));width:100%}', html)
        self.assertIn('.modal-overlay{padding:12px;align-items:flex-end;overflow-y:auto', html)
        self.assertIn('max-height:calc(100dvh - 24px', html)
        self.assertIn('.shadow-detail-panel{top:calc(42px', html)
        self.assertIn('background-attachment:scroll,scroll', html)
        self.assertIn('主导航不随当前页自动横向居中', html)
        self.assertIn('分区浮层仍由 navHighlight 负责把当前分区 tab 滚入可视区', html)

    def test_foldable_global_partition_does_not_resize_fixed_bottom_nav(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('</div>\n<nav class="mobilenav"><div class="mn-inner">', html)
        self.assertIn('.gseg{display:none;position:fixed;', html)
        self.assertIn('bottom:calc(70px + env(safe-area-inset-bottom,0px))', html)
        self.assertIn('主内容只为固定底栏留空间；分区条是浮层，不改变页面滚动几何。', html)
        self.assertNotIn('body[data-navtop="global"] .main{padding-bottom:calc(var(--mynav-h', html)
        self.assertNotIn('body[data-navtop="global"] .mobilenav{padding-top:', html)

    def test_foldable_primary_nav_uses_fixed_equal_slots(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('.mn-inner{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));width:100%}', html)
        self.assertIn('.mn-item{display:flex;min-width:0;', html)
        self.assertIn('.mn-item.mn-theme{margin-left:0}', html)
        self.assertNotIn('scroll-snap-type:x proximity', html)
        self.assertNotIn('min-width:max-content', html)

    def test_butler_keyboard_uses_one_visual_viewport_geometry_source(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('body.butler-kb-open #view-butler.active', html)
        self.assertIn("window.__syncButlerKeyboard=syncButlerKeyboard", html)
        self.assertIn("navigator.virtualKeyboard.overlaysContent=false", html)
        self.assertIn("vv.addEventListener('resize',scheduleButlerKeyboard)", html)
        self.assertIn("kbTimers=[0,60,180,360]", html)
        self.assertIn("view.style.setProperty('height',Math.round(height)+'px','important')", html)
        self.assertIn("['top','left','right','width','height'].forEach", html)
        self.assertIn('body.butler-kb-open #view-butler.active{', html)
        self.assertIn('transition:none!important;', html)
        self.assertIn('animation:none!important;', html)
        self.assertNotIn('@media (pointer:coarse){\n  body.butler-kb-open', html)
        self.assertNotIn("navigator.virtualKeyboard.overlaysContent=true", html)
        self.assertNotIn("body[data-vk] #view-butler.active", html)
        self.assertNotIn("body:has(#view-butler.active:focus-within)", html)

    def test_view_navigation_does_not_animate_page_scroll(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('html{scroll-behavior:auto}', html)
        self.assertIn("window.scrollTo({top:0,left:0,behavior:'auto'})", html)
        self.assertNotIn('setInterval(applyFoldLayout,3000)', html)
        self.assertIn('var foldOpen=false', html)
        self.assertIn('function scheduleFoldLayout()', html)

    def test_startup_async_loads_are_single_flight(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('await _bootApply(_p.d,true)', html)  # 快照回放走单飞 _bootApply（fromSnapshot=true 不续期时间戳）
        self.assertIn("readOnce('boot','/api/boot',30000)", html)
        self.assertIn("readOnce('health-range-'+days,'/api/health?days='+days", html)

    def test_charts_skip_unchanged_data(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function _chartSig(value)', html)
        self.assertGreaterEqual(html.count('__hermesSig'), 8)
        self.assertIn('cfg.options.animation=false', html)
    def test_today_view_exposes_decision_surface(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="today-focus-list"', html)
        self.assertIn('function renderTodayFocus()', html)
        self.assertIn('const shown=candidates.slice(0,5)', html)
        # IA-4：今日页为主栏 + 辅助栏。主栏保持重点→执行，状态带位于辅助栏顶部；
        # 窄屏由 CSS 将双栏折叠成单列，所有业务挂载点仍唯一存在。
        self.assertIn('class="today-layout"', html)
        self.assertIn('class="today-primary"', html)
        self.assertIn('class="today-aside"', html)
        self.assertIn('id="today-band"', html)
        self.assertIn('id="today-rock"', html)
        self.assertLess(html.index('id="today-rock"'), html.index('id="today-focus"'))
        self.assertEqual(html.count('id="today-band"'), 1)
        self.assertEqual(html.count('id="today-focus"'), 1)
        self.assertIn('#view-today.active .today-layout{display:grid', html)
        self.assertIn('#view-today.active .today-layout{grid-template-columns:1fr', html)
        self.assertIn('#view-today.active{display:flex;flex-direction:column}', html)
        self.assertIn("today-focus-more", html)
        self.assertIn('const LO=window.LO={', html)

    def test_today_visual_migration_has_complete_night_theme_surface(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        required=(
            'body.night-mode #view-today.active .today-rock-card',
            'body.night-mode #view-today.active .today-focus-card',
            'body.night-mode #view-today.active .today-aside .today-band',
            'body.night-mode #view-today.active .today-habit-card',
            'body.night-mode #view-today.active .today-capture-card',
            'body.night-mode #view-today.active .today-primary .quote-card',
            'body.night-mode #view-today.active .today-capture-card input',
            'body.night-mode #view-today.active .today-task .tt-tag-role',
        )
        for selector in required:
            self.assertIn(selector, html)
    def test_destructive_lifeos_actions_require_object_confirmation(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        pro=html[html.index('async function proDel'):html.index('async function proToTask',html.index('async function proDel'))]
        rel=html[html.index('async function relDel'):html.index('async function lisAdd',html.index('async function relDel'))]
        lis=html[html.index('async function lisDel'):html.index('// ═════════ 🌱',html.index('async function lisDel'))]
        self.assertIn('确认删除关注圈条目', pro)
        self.assertIn('确认删除与', rel)
        self.assertIn('确认删除与', lis)
    def test_coach_bar_reuses_unchanged_dom(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('function renderCoachBar(view)')
        end=html.index('/* ═══════════ P4',start)
        block=html[start:end]
        self.assertIn('old.dataset.coachId===String(c.id)', block)
        self.assertIn('bar.dataset.coachId=String(c.id)', block)
    def test_influence_to_task_conversion_is_idempotent(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('const _proTaskBusy=new Set()')
        end=html.index('async function proSaveCheckin',start)
        block=html[start:end]
        self.assertIn('x.converted||_proTaskBusy.has(id)', block)
        self.assertIn("!r.task||!r.task.id", block)
        self.assertIn('x.converted=true;x.taskId=r.task.id', block)
        self.assertIn('saveProactive({concerns:LO.pro.concerns})', block)
        self.assertIn('任务已创建，但“已转任务”标记未同步', block)
    def test_service_worker_refreshes_scripts_from_network(self):
        sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        self.assertIn("if (url.pathname.endsWith('.js'))", sw)
        self.assertIn("fetch(e.request, {cache:'no-store'})", sw)
        self.assertIn(".catch(() => caches.match(e.request))", sw)
        self.assertIn("'./frontend-modules/api-client.js'", sw)
    def test_shared_api_client_is_loaded_with_fallback(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("window.__apiClientReady=import('./frontend-modules/api-client.js')", html)
        self.assertIn('window.__apiClient.requestJSON', html)
        self.assertIn("window.__sseClientReady=import('./frontend-modules/sse-client.js')", html)

    def test_durable_write_queue_and_recovery_console_are_wired(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        module=(ROOT/'frontend-modules'/'write-coordinator.js').read_text(encoding='utf-8')
        sync=(ROOT/'frontend-modules'/'sync-ui.js').read_text(encoding='utf-8')
        sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        backend=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn("import('./frontend-modules/write-coordinator.js')", html)
        self.assertIn('dash_write_outbox_v1', module)
        self.assertIn('async function retryAll()', module)
        self.assertIn('只手动重试，不静默覆盖', sync)
        self.assertIn('数据恢复点', sync)
        self.assertIn('发布与自动任务', sync)
        self.assertIn("fetchOps:()=>api('GET','api/ops/status')", html)
        self.assertIn("'./frontend-modules/write-coordinator.js'", sw)
        self.assertIn('"/api/mutations/recent"', backend)
        self.assertIn('"/api/recovery/status"', backend)
        self.assertIn('"/api/recovery/restore"', backend)
        self.assertIn('"/api/ops/status"', backend)
        self.assertIn('api/butler/act', html)
    def test_butler_cards_preserve_existing_idempotency_key(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertGreaterEqual(html.count("const id=a._id||('pv-"), 2)

    def test_sync_detail_panel_and_ai_receipts_are_visible(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function renderSyncDetailPanel', html)
        self.assertIn('function toggleSyncDetail', html)
        self.assertIn('点击查看同步详情', html)
        self.assertIn('function showActionReceipt', html)
        self.assertIn("undoButlerAction(rec.clientTurnId)", html)
        self.assertIn('有本地修改待同步', html)
        self.assertIn('等待处理多端冲突', html)
        self.assertIn('已保存到服务器', html)
        self.assertIn('retryButlerAction', html)

    def test_ai_undo_is_bound_to_receipt(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("api('POST','/api/butler/undo',clientTurnId?{clientTurnId}:{}", html)
        self.assertIn("'api/butler/undo'", html)
        self.assertIn('已保存到服务器', html)
        self.assertIn('有本地修改待同步', html)
        self.assertIn('等待处理多端冲突', html)
    def test_shared_sse_module_is_cached_by_service_worker(self):
        sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        self.assertIn("'./frontend-modules/sse-client.js'", sw)
        module=(ROOT/'frontend-modules'/'sse-client.js').read_text(encoding='utf-8')
        self.assertIn('export async function streamSSE', module)
        self.assertIn('AbortController', module)
        self.assertIn("'./frontend-modules/sync-ui.js'", sw)
    def test_butler_page_uses_shared_sse_parser(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("const acc=await streamSSE('/api/butler/stream'", html)
        self.assertNotIn('processBufPage', html)
    def test_local_state_conflict_has_recovery_actions(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id:\'sync-conflict-bar\'', html)
        self.assertIn('function showSyncConflict', html)
        self.assertIn('function applyRemoteConflict', html)
        self.assertIn('function mergeRemoteConflict', html)
        self.assertIn('合并本地修改', html)
        self.assertIn('采用服务器版本', html)
        self.assertIn('保留本地修改', html)
        self.assertIn('showSyncConflict(r)', html)
        self.assertIn('function localConflictSummary', html)
        self.assertIn('冲突字段：', html)
        self.assertIn('function scheduleSyncRetry', html)
    def test_local_snapshot_tracks_revision_and_dirty_state(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function localStateSnapshot()', html)
        self.assertIn('_revision:Number(S._localRevision||0)', html)
        self.assertIn('_dirty:!!S._localDirty', html)
        self.assertIn('if(S._localDirty){', html)
        self.assertIn('showSyncConflict({conflict:true,serverRevision:remoteRevision,state:d})', html)
        self.assertIn('S._localDirty=false;saveLocalSnapshot()', html)
        self.assertIn('_base:S._localBaseState?cloneLocal(S._localBaseState):null', html)
        self.assertIn('S._localBaseState=cloneLocal(localStateData())', html)
    def test_network_status_is_visible_for_transport_failures(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="network-status"', html)
        self.assertIn("window.addEventListener('dash-network'", html)
        self.assertIn("window.addEventListener('offline'", html)
        api_client=(ROOT/'frontend-modules'/'api-client.js').read_text(encoding='utf-8')
        mutations=(ROOT/'frontend-modules'/'task-mutations.js').read_text(encoding='utf-8')
        self.assertGreaterEqual(api_client.count("dash-network"),2)
        self.assertGreaterEqual(mutations.count("dash-network"),2)
    def test_all_task_removal_ui_uses_reversible_archive(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertNotIn("/api/tasks/delete", html)
        self.assertIn("async function confirmDeleteTask", html)
        self.assertIn("async function rockDel", html)
        self.assertGreaterEqual(html.count("/api/tasks/archive"),3)
        self.assertIn('可随时恢复', html)
    def test_completed_task_undo_syncs_ticktick_status(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('async function undoTask', html)
        self.assertIn("/api/tasks/update',{id:c.id,projectId:c.pid,status:0}", html)
        self.assertIn('已撤销完成并同步', html)
    def test_quadrant_move_persists_and_rolls_back_on_failure(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('async function moveQuad', html)
        self.assertIn('t.quad=targetQuad;t.content=content;', html)
        self.assertIn('persist();reconcileAfterMutation();', html)
        self.assertIn("t.quad=oldQuad;t.content=oldContent;persist();renderActions();", html)
    def test_completion_reconciles_only_after_remote_success(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function completeAction')
        end=html.index('async function delAction', start)
        block=html[start:end]
        self.assertNotIn("toast('✅ 搞定！');reconcileAfterMutation()", block)
        self.assertIn('if(r.success){persist();reconcileAfterMutation();}', block)

    def test_task_update_uses_backend_readback_contract(self):
        source=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn('verify_fields = {k: t[k]', source)
        self.assertIn('回读未验证到任务更新', source)

    def test_load_data_clears_tasks_when_remote_list_is_empty(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function loadData')
        end=html.index('async function loadHabits', start)
        block=html[start:end]
        self.assertIn('if(Array.isArray(d.tasks))', block)
        self.assertNotIn('if(d.tasks?.length)', block)
        self.assertIn('if(Array.isArray(d.roles))', block)
        self.assertIn("!Array.isArray(d.roles)", block)

    def test_boot_load_falls_back_when_aggregated_core_payload_is_partial(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('function _bootApply')
        end=html.index('// ═══════════ 任务状态判断', start)
        block=html[start:end]
        self.assertIn('const coreOk=', block)
        self.assertIn('await _bootApply(d)', block)
        self.assertIn('return false;', block)
    def test_save_all_checks_each_role_sync_result(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function saveAll')
        end=html.index('// ═══════════ 角色详情弹窗', start)
        block=html[start:end]
        self.assertIn('const failed=[];let saved=0;', block)
        self.assertIn('if(r&&r.success){delete S.dirty[k];saved++;}else failed.push(k);', block)
        self.assertIn('部分未保存', block)
        self.assertIn('已保留待保存状态', block)
    def test_role_rename_save_tolerates_missing_old_names_map(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function saveAll')
        end=html.index('// ═══════════ 角色详情弹窗', start)
        block=html[start:end]
        self.assertIn('((S.oldNames||{})[roleId]||role.name)', block)

    def test_role_emoji_and_review_are_marked_dirty_for_persistence(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function saveAll')
        end=html.index('// ═══════════ 角色详情弹窗', start)
        block=html[start:end]
        self.assertIn("k.startsWith('emoji_')", block)
        self.assertIn("payload.emoji=role.emoji||'📌'", block)
        self.assertIn("S.dirty['review_'+r.id]=true", html)
        self.assertIn("S.dirty['emoji_'+rid]=true", html)
    def test_proactive_edit_rolls_back_when_save_fails(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function proEditConcern')
        end=html.index('async function proLogEdit', start)
        block=html[start:end]
        self.assertIn('const oldText=x.text', block)
        self.assertIn('const ok=await saveProactive', block)
        self.assertIn('已恢复原内容', block)

    def test_proactive_move_delete_and_checkin_handle_save_failures(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        move=html[html.index('async function proMove'):html.index('async function proDel')]
        delete=html[html.index('async function proDel'):html.index('async function proToTask')]
        check=html[html.index('async function proSaveCheckin'):html.index('async function proAddLog')]
        self.assertIn('const oldCircle=x.circle', move)
        self.assertIn('已恢复原位置', move)
        self.assertIn('const removed=LO.pro.concerns[idx]', delete)
        self.assertIn("/api/proactive/delete',{id,kind:'concern'}", delete)
        self.assertIn('已恢复原条目', delete)
        self.assertIn('const hadOwn=Object.prototype.hasOwnProperty.call', check)
        self.assertIn('已恢复原记录', check)

    def test_habit_role_save_surfaces_failure(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function habitSetRole')
        end=html.index('let currentHeatHid', start)
        block=html[start:end]
        self.assertIn('习惯归属保存失败', block)

    def test_capture_actions_surface_network_failures(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        quick=html[html.index('async function quickCapture'):html.index('function persistCapRole', html.index('async function quickCapture'))]
        cmd=html[html.index('async function cmdkCapture'):html.index("document.getElementById('cmdk-capture-input')", html.index('async function cmdkCapture'))]
        self.assertIn('收集失败（网络）', quick)
        self.assertIn('收集失败（网络）', cmd)

    def test_habit_rename_carries_old_name_for_dimension_migration(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function habitEdit')
        end=html.index('async function habitDel', start)
        self.assertIn('oldName:h.name', html[start:end])

    def test_boolean_habit_checkin_toggles_remote_value(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function checkinHabit')
        end=html.index('function renderHabits', start)
        self.assertIn("h.type==='Boolean'&&h.checked)?0:1", html[start:end])
        self.assertIn('打卡失败（网络）', html[start:end])
        self.assertIn("(r&&r.error)||'打卡失败'", html[start:end])

    def test_habit_create_surfaces_partial_dimension_warning(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("r.warning?'⚠️ '", html)

    def test_week_plan_create_handles_network_failure(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function rockAdd')
        end=html.index('async function rockDone', start)
        block=html[start:end]
        self.assertIn("写入失败（网络）", block)
        self.assertIn("if(r&&r.success)", block)
        self.assertIn("btn.disabled=true", block)

    def test_primary_local_edit_forms_handle_network_failures(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        checks=(
            ('async function saveReview', 'async function createRole', '保存失败（网络）'),
            ('async function createRole', '// ═══════════ 保存按钮', '创建角色失败（网络）'),
            ('async function relEdit', 'async function lisEdit', '更新失败（网络）'),
            ('async function lisEdit', 'async function proEditConcern', '更新失败（网络）'),
            ('async function proLogEdit', 'async function proLogDel', '更新失败（网络）'),
            ('async function proLogDel', 'async function habitEdit', '删除失败（网络）'),
            ('async function habitEdit', 'async function habitDel', '更新失败（网络）'),
            ('async function roleDel', '// ═════════ 五链路健康条', '删除失败（网络）'),
            ('async function relAdd', 'async function relDel', '保存失败（网络）'),
            ('async function relDel', 'async function lisAdd', '删除失败（网络）'),
            ('async function lisAdd', 'async function lisDel', '保存失败（网络）'),
            ('async function lisDel', '// ═════════ 🌱 习惯七', '删除失败（网络）'),
        )
        for start_marker,end_marker,message in checks:
            start=html.index(start_marker); end=html.index(end_marker,start)
            self.assertIn(message,html[start:end])
        rel=html[html.index('async function relAdd'):html.index('async function relDel')]
        lis=html[html.index('async function lisAdd'):html.index('async function lisDel')]
        self.assertIn('btn.disabled=true',rel)
        self.assertIn('btn.disabled=true',lis)

    def test_relation_and_listening_create_show_mirror_warning(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        rel=html[html.index('async function relAdd'):html.index('async function relDel')]
        lis=html[html.index('async function lisAdd'):html.index('async function lisDel')]
        self.assertIn('r.warning',rel)
        self.assertIn('r.warning',lis)

    def test_relation_and_listening_edit_delete_show_mirror_warning(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        for start_marker,end_marker in (
            ('async function relEdit','async function lisEdit'),
            ('async function lisEdit','async function proEditConcern'),
            ('async function relDel','async function lisAdd'),
            ('async function lisDel','// ═════════ 🌱 习惯七'),
        ):
            start=html.index(start_marker); end=html.index(end_marker,start)
            self.assertIn('r.warning',html[start:end])

    def test_role_batch_save_isolates_network_failure_per_item(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function saveAll')
        end=html.index('// ═══════════ 角色详情弹窗',start)
        block=html[start:end]
        self.assertIn("catch(e){failed.push(k);continue;}",block)
        self.assertIn('部分未保存',block)

    def test_proactive_add_and_log_roll_back_on_failure(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        add=html[html.index('async function proAdd'):html.index('async function proMove')]
        log=html[html.index('async function proAddLog'):html.index('/* ===== Life OS 扩展 part2 ===== */')]
        task=html[html.index('async function proToTask'):html.index('async function proSaveCheckin')]
        self.assertIn('内容已保留可重试',add)
        self.assertIn('LO.pro.concerns.splice',add)
        self.assertIn('内容已保留可重试',log)
        self.assertIn('LO.pro.logs.splice',log)
        self.assertIn('写入失败（网络）',task)

    def test_proactive_save_tracks_mirror_warning(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function saveProactive')
        end=html.index('async function reloadChain',start)
        self.assertIn('_proactiveMirrorWarning',html[start:end])
        check=html[html.index('async function proSaveCheckin'):html.index('async function proAddLog')]
        self.assertIn('_proactiveMirrorWarning',check)

    def test_proactive_edit_delete_and_health_notes_surface_failures(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        concern=html[html.index('async function proEditConcern'):html.index('async function proLogEdit')]
        log_edit=html[html.index('async function proLogEdit'):html.index('async function proLogDel')]
        log_del=html[html.index('async function proLogDel'):html.index('async function habitEdit')]
        concern_del=html[html.index('async function proDel'):html.index('async function proToTask')]
        self.assertIn('_proactiveMirrorWarning',concern)
        self.assertIn('r.warning',log_edit)
        self.assertIn('r.warning',log_del)
        self.assertIn('_proactiveMirrorWarning',concern_del)
        notes=html[html.index('async function loadHealthNotes'):html.index('function renderHealthNotes')]
        self.assertIn('let failed=false',notes)
        self.assertIn('健康备注暂时无法加载',notes)
        self.assertIn('LO.healthNotes||[]',notes)

    def test_butler_proactive_actions_surface_warning_and_refresh(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        preview=html[html.index("const w=r.warning||'';", html.index('function butlerPreview')):html.index('function advShowActs')]
        self.assertIn("已存入：'+cfg.name",preview)
        self.assertIn("w?'warn':'success'",preview)
        after=html[html.index('async function butlerAfterAct'):html.index('// ═════════ 习惯创建', html.index('async function butlerAfterAct'))]
        self.assertIn("kind==='删关注'||kind==='改关注'",after)
        self.assertIn("/api/proactive",after)

    def test_destructive_butler_actions_require_explicit_confirmation(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function confirmButlerDestructive(kind,args)',html)
        self.assertIn("const destructive=['删任务','删关系','删倾听','删角色','删关注']",html)
        self.assertGreaterEqual(html.count('confirmButlerDestructive('),4)

    def test_role_week_action_push_saves_pushed_marker_immediately(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        block=html[html.index('async function pushWeekAct'):html.index('// ═══════════ 拖拽系统',html.index('async function pushWeekAct'))]
        self.assertIn('await saveAll()',block)
        self.assertIn("S.dirty['weekAct_'+r.id]",block)
        self.assertIn('任务已创建，但角色行动标记待同步',block)

    def test_render_scheduler_batches_domain_updates(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function scheduleRender(...domains)',html)
        self.assertIn('_renderFrame=requestAnimationFrame(flushRenderQueue)',html)
        self.assertIn("scheduleRender(o?[o]:['tasks','habits','roles','stats'])",html)

    def test_high_frequency_mutations_patch_only_changed_dom(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        complete=html[html.index('async function completeAction'):html.index('async function delAction')]
        checkin=html[html.index('async function checkinHabit'):html.index('function habitCardHTML')]
        self.assertIn('patchTaskRemoved(taskId)',complete)
        self.assertNotIn('renderAll()',complete)
        self.assertIn('patchHabitDom(h)',checkin)
        self.assertNotIn('renderHabits()',checkin)

    def test_sky_engine_runs_only_in_visible_stargazing_view(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("var n=document.body.classList.contains('stargazing')&&!document.hidden",html)
        self.assertIn("document.addEventListener('visibilitychange',sync)",html)
        self.assertIn('window.__skyRunning=false',html)

    def test_stargaze_controls_and_oracle_are_inert_outside_stargaze(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('#sg-exit{position:fixed;right:26px;top:20px;z-index:4002;opacity:0;pointer-events:none;visibility:hidden;',html)
        self.assertIn('body.stargazing #sg-exit{opacity:1;pointer-events:auto;visibility:visible}',html)
        self.assertIn('body.stargazing #sg-oracle{visibility:visible}',html)
        self.assertIn('body.stargazing #sg-oracle.show{opacity:1}',html)
        self.assertNotIn('\n#sg-oracle.show{opacity:1}',html)

    def test_decorative_effects_stop_when_hidden_or_motion_reduced(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        ripple=html[html.index('// ═══════════ 水波涟漪'):html.index('// ═══════════ 彩带庆祝')]
        confetti=html[html.index('// ═══════════ 彩带庆祝'):html.index('// ═══════════ 数字滚动动画')]
        self.assertIn("matchMedia('(pointer:fine)').matches",ripple)
        self.assertIn("document.addEventListener('visibilitychange'",ripple)
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)').matches",confetti)
        self.assertIn("document.addEventListener('visibilitychange'",confetti)

    def test_health_chart_queue_cancels_outside_health_view(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('function _hcCancel()',html)
        self.assertIn("id!=='view-health'",html)
        switch=html[html.index('function switchView'):html.index('/* ═══════════ P3',html.index('function switchView'))]
        self.assertIn("name!=='health'",switch)

    def test_read_requests_are_single_flight_and_latest_health_range_wins(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('const _readFlights=new Map()',html)
        self.assertIn('function readOnce(key,route,timeout=15000,fresh=false)',html)
        self.assertIn("old.controller.abort()",html)
        health=html[html.index('async function loadHealth'):html.index('function healthSetDays')]
        self.assertIn("readOnce('health-range-'+days",health)
        self.assertNotIn("api('GET','/api/health",health)
        setdays=html[html.index('function healthSetDays'):html.index('async function renderHealth')]
        self.assertIn('if(d&&S.healthWant===n)',setdays)

    def test_health_first_open_commits_default_range_before_awaiting_data(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        state=html[html.index('const S='):html.index('const COLORS=',html.index('const S='))]
        render=html[html.index('async function renderHealth'):html.index('// ── 健康页 AI 三件套',html.index('async function renderHealth'))]
        refresh=html[html.index('async function healthRefresh'):html.index('async function renderHealth')]
        self.assertIn('healthWant:7',state)
        self.assertLess(render.index('S.healthWant=days'),render.index('await loadHealth(days)'))
        self.assertIn("dash_health_snap",render)
        self.assertIn('S.healthWant=n',refresh)
        boot=html[html.index('snapRestore();renderAll()'):html.index('const _bootRun=bootLoad()',html.index('snapRestore();renderAll()'))]
        self.assertIn("id==='view-health')renderHealth()",boot)

    def test_stale_health_range_cannot_replace_current_selection(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        health=html[html.index('async function loadHealth'):html.index('let _healthWatchTimer',html.index('async function loadHealth'))]
        self.assertIn('if(Number(S.healthWant||days)===days)',health)
        self.assertIn('return d;',health)

    def test_health_auxiliary_cards_do_not_refetch_or_rebuild_on_every_entry(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        aux=html[html.index('let _healthBriefLoaded'):html.index('// ── 🫁',html.index('let _healthBriefLoaded'))]
        corr=html[html.index('async function renderHealthCorr'):html.index('// 分帧建图',html.index('async function renderHealthCorr'))]
        self.assertIn('_healthBriefLoaded&&!force',aux)
        self.assertIn('_healthBehaviorLoaded&&!force',aux)
        self.assertIn('_healthNotesLoaded&&!force',aux)
        self.assertIn('body.dataset.sig',aux)
        self.assertIn('box.dataset.sig',aux)
        self.assertIn("readOnce('health-correlation-14'",corr)
        self.assertIn('_healthCorrLoaded&&!force',corr)
        self.assertIn("let _healthRenderSig=''",html)
        self.assertIn('renderSig===_healthRenderSig',html)

    def test_dashboard_reload_accepts_remote_role_and_quadrant_authority(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        load=html[html.index('async function loadData'):html.index('async function loadHabits',html.index('async function loadData'))]
        self.assertIn('TickTick 正文是角色/象限权威源',load)
        self.assertNotIn('_oldT.quad=_pq',load)
        self.assertNotIn('_oldT.role=_pr',load)

    def test_global_refresh_keeps_health_default_range_at_seven_days(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('async function refreshData')
        end=html.index('\n',start)
        block=html[start:end]
        self.assertIn('loadHealth(S.healthWant||7,true)',block)
        self.assertNotIn('loadHealth(S.healthWant||14,true)',block)

    def test_today_health_strip_does_not_force_background_refresh(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        # IA-3：今日健康口径收编进 renderTodayBand 状态带（原 health-strip 移除），契约随迁
        start=html.index('/* ═══════ IA-3 L0')
        end=html.index('function renderTodayRock',start)
        block=html[start:end]
        self.assertNotIn('loadHealth(S.healthWant||7,true)',block)
        self.assertIn('!_fetched&&!window.__bootPhase',block)
        self.assertIn('_todayHealthFetch',block)
        self.assertIn('_todayHealthRetryAt',block)
        self.assertIn('后台拉一次不阻塞首屏',block)

    def test_health_rides_accept_localized_workout_names(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        block=html[html.index('function renderHealthRides'):html.index('async function renderHealthCorr',html.index('function renderHealthRides'))]
        self.assertIn("w.name||w.type||w.activityType", block)
        self.assertIn('bike|bicycle|cycle|cycling', block)
        self.assertIn("distance_source==='gps'", block)
        self.assertIn('距离未提供', block)
        self.assertIn('speed_source===\'derived\'', block)
        self.assertIn('heart_rate_avg', block)
        self.assertIn('workout_quality', block)

    def test_health_advisor_collects_normalized_workout_fields(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        block=html[html.index("health:{\n      title:'健康看板'"):html.index("      quick:", html.index("health:{\n      title:'健康看板'"))]
        self.assertIn('km:w.distance_km', block)
        self.assertIn('minutes:w.duration_min', block)
        self.assertIn('speed:w.speed_kmh', block)
        self.assertIn('intensity:w.intensity', block)
        self.assertIn('freshness:{window_days', block)
        self.assertIn('advisorEvidenceLine', html)

    def test_health_derived_receives_full_workout_window(self):
        server=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn('"workouts": _all_workouts', server)
        self.assertIn('按自然日而非“前 N 条记录”计算', server)

    def test_health_coverage_distinguishes_ingest_from_parser_gaps(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        block=html[html.index('function renderHealthCoverage'):html.index('function renderHealthReadiness', html.index('function renderHealthCoverage'))]
        self.assertIn('HAE 原始推送未出现', block)
        self.assertIn('原始推送已有但驾驶舱当前窗口未解析', block)
        self.assertIn('metric_latest_data_date', block)
        self.assertIn('latest_receive_at', block)
        self.assertIn('HTTPS 已就绪', block)

    def test_health_coverage_surfaces_per_metric_receive_freshness(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        block=html[html.index('function renderHealthCoverage'):html.index('function renderHealthReadiness', html.index('function renderHealthCoverage'))]
        self.assertIn('metric_last_receive_at', block)
        self.assertIn('逐项最近接收时间', block)
        self.assertIn('metric_point_counts', block)
        self.assertIn('hc-lastseen-grid', html)
        self.assertIn('workout_missing_distance', block)
        self.assertIn('workout_with_route', block)
        self.assertIn('workout_latest_start', block)

    def test_closed_loop_gaps_are_actionable_from_today(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        backend=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn('id="fe-role"', html)
        self.assertIn("role!==_oldR", html)
        self.assertIn("data-type=", html)
        self.assertIn("type==='task_meta'&&id", html)
        self.assertIn('"tasksActionable"', backend)
        self.assertIn('chain_health["gaps"]', backend)
        self.assertIn('"habit_meta"', backend)

    def test_advisor_has_evidence_boundary_and_outcome_feedback(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        backend=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn('function advAttachFeedback', html)
        self.assertIn("advisorFeedback('actioned'", html)
        self.assertIn("'/api/advisor/outcomes'", html)
        self.assertIn('证据边界', html)
        self.assertIn('"adviceId": advice_id', backend)
        self.assertIn('不保存用户快照、问题或 AI 正文', backend)
        self.assertIn('advisor_outcomes', backend)

    def test_health_watch_never_repaints_automatically(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        watch=html[html.index('async function healthWatch'):html.index('function syncHealthWatch', html.index('async function healthWatch'))]
        self.assertIn('healthSetPending(true)', watch)
        self.assertNotIn('loadHealth(', watch)
        self.assertNotIn('renderHealthAll(', watch)
        self.assertIn('health-refresh.pending', html)

    def test_health_extra_metrics_have_collapsed_mobile_safe_surface(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="health-extra-card"', html)
        self.assertIn('function renderHealthExtras(d)', html)
        self.assertIn("renderHealthExtras(d);", html)
        self.assertIn('.health-extra-card>summary', html)
        self.assertIn('min-height:44px', html)
        self.assertIn('v.units', html)
        self.assertIn("replace(/^dBASPL$/i,'dB')", html)

    def test_health_coverage_diagnosis_surfaces_missing_and_partial_days(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="health-coverage-card"', html)
        self.assertIn('function renderHealthCoverage(d)', html)
        self.assertIn('仅部分天有记录', html)
        self.assertIn('Health Auto Export', html)
        self.assertIn('q.stale', html)
        self.assertIn('数值异常', html)

    def test_boot_payload_eliminates_life_os_startup_waterfall(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        server=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        for key in ('listening','proactive','week_plan','chain_health'):
            self.assertIn(f'_safe("{key}"',server)
            self.assertIn(f"fromBoot('{key}'",html)
        self.assertIn('window.__bootPhase',html)
        self.assertIn('window.__bootMetrics',html)
        self.assertIn('setTimeout(r,8000)',html)
        today=html[html.index('function renderTodayBand'):html.index('function renderTodayFocus')]
        self.assertIn('!window.__bootPhase',today)
        self.assertIn('S.healthDays=1',html)

    def test_lifeos_renderall_wrapper_is_frame_batched(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('const _rAll=window.renderAll')
        end=html.index('const _refresh=window.refreshData',start)
        block=html[start:end]
        self.assertIn('let _extraRenderFrame=0',block)
        self.assertIn('if(!_extraRenderFrame)_extraRenderFrame=requestAnimationFrame',block)
        self.assertNotIn('try{renderChain();}catch(e){}return r;',block)

    def test_foldable_touch_scroll_uses_no_backdrop_resampling(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('@media (pointer:coarse) and (max-width:860px)')
        end=html.index('/* ═══ 苹果风收尾',start)
        block=html[start:end]
        self.assertIn('backdrop-filter:none!important',block)
        self.assertIn('.view.active{animation:none!important}',block)
        self.assertIn('.card,.oracle-panel,.stat-card',block)

    def test_background_intervals_stop_while_page_hidden(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('clearInterval(vpsTimer)',html)
        self.assertIn('clearInterval(themeTimer)',html)
        self.assertIn("document.addEventListener('visibilitychange',syncVpsTimer",html)
        self.assertIn("document.addEventListener('visibilitychange',syncThemeTimer",html)

    def test_reduced_motion_disables_decorative_animation(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        start=html.index('@media (prefers-reduced-motion: reduce)')
        end=html.index('/* ═══ 苹果风收尾',start)
        block=html[start:end]
        self.assertIn('animation-duration:.001ms!important',block)
        self.assertIn('scroll-behavior:auto!important',block)
        self.assertIn('.stargaze-canvas,.sky-canvas,.starfield-canvas',block)

    def test_view_switch_respects_reduced_motion_for_mobile_nav(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn("behavior:reduce?'auto':'smooth'",html)

    def test_touch_lists_skip_offscreen_paint(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        self.assertIn('.action-item,.habit-card,.role-ov-card{content-visibility:auto',html)
        self.assertIn('contain-intrinsic-size:1px 92px',html)
        self.assertIn('#ripple-canvas,#confetti-canvas{display:none!important}',html)

    def test_health_view_boundary_is_loaded_and_keeps_seven_day_fallback(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        module=(ROOT/'frontend-modules'/'health-view.js').read_text(encoding='utf-8')
        sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        self.assertIn("window.__healthViewReady=import('./frontend-modules/health-view.js')",html)
        self.assertIn('export function healthDays',module)
        self.assertIn('DEFAULT_HEALTH_DAYS = 7',module)
        self.assertIn("'./frontend-modules/health-view.js'",sw)

    def test_sync_panel_fetches_conflict_summary(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        module=(ROOT/'frontend-modules'/'sync-ui.js').read_text(encoding='utf-8')
        self.assertIn("fetchConflicts:()=>api('GET','api/sync/conflicts')",html)
        self.assertIn('fetchConflicts',module)
        self.assertIn('待人工确认的并发冲突',module)

    def test_sync_conflict_ack_is_explicitly_non_destructive(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        module=(ROOT/'frontend-modules'/'sync-ui.js').read_text(encoding='utf-8')
        server=(ROOT/'dashboard-server.py').read_text(encoding='utf-8')
        self.assertIn("api/sync/conflicts/ack", html)
        self.assertIn('ackConflict', module)
        self.assertIn('只隐藏当前提示，不会覆盖 Mac/VPS 数据', module)
        self.assertIn('def _ack_sync_conflict', server)
        self.assertIn('未修改任一端数据', server)

    def test_today_and_habits_boundaries_are_loaded_with_fallbacks(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        today=(ROOT/'frontend-modules'/'today-view.js').read_text(encoding='utf-8')
        habits=(ROOT/'frontend-modules'/'habits-view.js').read_text(encoding='utf-8')
        sw=(ROOT/'sw.js').read_text(encoding='utf-8')
        self.assertIn("window.__todayViewReady=import('./frontend-modules/today-view.js')",html)
        self.assertIn("window.__habitsViewReady=import('./frontend-modules/habits-view.js')",html)
        self.assertIn('export function greetingForHour',today)
        self.assertIn('export function habitScore',habits)
        self.assertIn("'./frontend-modules/today-view.js'",sw)
        self.assertIn("'./frontend-modules/habits-view.js'",sw)

    def test_runtime_audit_followups_are_guarded(self):
        html=(ROOT/'hermes-dashboard.html').read_text(encoding='utf-8')
        scripts=re.findall(r'<script(?![^>]*application/json)[^>]*>(.*?)</script>',html,re.S)
        main=max(scripts,key=len)
        self.assertNotIn('@media (prefers-reduced-motion',main)
        self.assertIn('!Array.isArray(days)||!Array.isArray(counts)||days.length!==counts.length',main)
        self.assertIn('function habitWeeklyGoal(h)',main)
        self.assertNotIn("h.name.includes('骑行')?300",main)
        self.assertNotIn("(h.name||'').includes('骑行')?300",main)
        self.assertIn('if(!btn||_oracleRefreshInFlight)return',main)
        self.assertIn('finally{_oracleRefreshInFlight=false',main)

if __name__=='__main__': unittest.main()
