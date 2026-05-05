const { createApp, ref, reactive, onMounted, onUnmounted } = Vue;

const app = createApp({
    setup() {
        const newTask = reactive({ scene: "up_info", input_value: "" });
        const submitting = ref(false);
        const tasks = ref([]);
        const taskTotal = ref(0);
        const taskPage = ref(1);
        const taskPageSize = 10;

        const selectedTask = ref(null);
        const taskDetail = reactive({ task: null, url_records: [], up_infos: [], video_infos: [], comments: [] });
        const wsProgress = reactive({ percent: 0, message: "" });
        let wsConnection = null;
        let wsHeartbeat = null;
        let _lastWsUpdate = 0;

        function statusTagClass(status) {
            const m = { pending: "tag-blue", running: "tag-yellow", processing: "tag-yellow", completed: "tag-green", partial: "tag-orange", failed: "tag-red" };
            return m[status] || "tag-blue";
        }
        function statusLabel(status) {
            const m = { pending: "等待中", running: "运行中", processing: "处理中", completed: "已完成", partial: "异常", failed: "失败" };
            return m[status] || status;
        }

        async function loadTasks() {
            try {
                const res = await axios.get("/api/tasks?page=" + taskPage.value + "&page_size=" + taskPageSize);
                tasks.value = res.data.items || [];
                taskTotal.value = res.data.total || 0;
            } catch (e) {
                ElementPlus.ElMessage.error("加载任务列表失败");
            }
        }

        async function submitTask() {
            if (!newTask.input_value.trim()) { ElementPlus.ElMessage.warning("请输入UID或BV号"); return; }
            submitting.value = true;
            try {
                const res = await axios.post("/api/tasks", { scene: newTask.scene, input_value: newTask.input_value.trim() });
                ElementPlus.ElMessage.success("任务已创建: " + res.data.id);
                newTask.input_value = "";
                await loadTasks();
                viewTask(res.data);
            } catch (e) {
                ElementPlus.ElMessage.error("创建任务失败: " + (e.response?.data?.detail || e.message));
            }
            submitting.value = false;
        }

        async function viewTask(task) {
            selectedTask.value = task;
            wsProgress.percent = task.total_urls > 0 ? Math.round(task.completed_urls / task.total_urls * 100) : 0;
            wsProgress.message = "";
            Object.assign(taskDetail, { task: null, url_records: [], up_infos: [], video_infos: [], comments: [] });
            try {
                const res = await axios.get("/api/tasks/" + task.id + "/results");
                Object.assign(taskDetail, res.data);
                wsProgress.percent = res.data.task.total_urls > 0 ? Math.round(res.data.task.completed_urls / res.data.task.total_urls * 100) : 0;
                wsProgress.message = res.data.progress_message || "";
            } catch (e) { console.error(e); }
            connectWebSocket(task.id);
        }

        async function loadTaskDetail(taskId) {
            try {
                const res = await axios.get("/api/tasks/" + taskId + "/results");
                Object.assign(taskDetail, res.data);
                if (res.data.progress_message) {
                    wsProgress.message = res.data.progress_message;
                }
            } catch (e) { console.error(e); }
        }

        let _wsTaskId = null, _wsReconnectTimer = null, _wsRetries = 0;

        function connectWebSocket(taskId) {
            if (wsConnection) { wsConnection.close(); wsConnection = null; }
            if (wsHeartbeat) { clearInterval(wsHeartbeat); wsHeartbeat = null; }
            if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
            _wsTaskId = taskId;
            _wsRetries = 0;
            _doConnect();
        }

        function _doConnect() {
            if (!_wsTaskId) return;
            const wsUrl = (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws/tasks/" + _wsTaskId;
            wsConnection = new WebSocket(wsUrl);
            wsConnection.onmessage = function (event) {
                _wsRetries = 0;
                try {
                    const d = JSON.parse(event.data);
                    if (d.type === "pong") return;
                    if (d.type === "progress" || d.type === "complete") {
                        if (d.total_urls > 0) {
                            wsProgress.percent = Math.round(d.completed_urls / d.total_urls * 100);
                        }
                        wsProgress.message = d.message || "";
                        loadTaskDetail(d.task_id);
                        loadTasks();
                    }
                } catch (e) { console.error("WS error:", e); }
            };
            wsConnection.onopen = function () {
                // 重连后立即同步最新状态
                if (_wsTaskId) {
                    loadTaskDetail(_wsTaskId);
                    loadTasks();
                }
            };
            wsHeartbeat = setInterval(function () {
                if (wsConnection && wsConnection.readyState === WebSocket.OPEN) wsConnection.send("ping");
            }, 30000);
            wsConnection.onclose = function () {
                if (wsHeartbeat) { clearInterval(wsHeartbeat); wsHeartbeat = null; }
                wsConnection = null;
                // 自动重连, 最多 10 次, 间隔递增
                if (_wsTaskId && _wsRetries < 10) {
                    var delay = Math.min(1000 * Math.pow(2, _wsRetries), 30000);
                    _wsRetries++;
                    _wsReconnectTimer = setTimeout(_doConnect, delay);
                }
            };
        }

        function closeDetail() {
            selectedTask.value = null;
            if (wsConnection) { wsConnection.close(); wsConnection = null; }
            if (wsHeartbeat) { clearInterval(wsHeartbeat); wsHeartbeat = null; }
        }

        async function deleteTask(taskId) {
            try {
                await ElementPlus.ElMessageBox.confirm("确定删除该任务及所有采集数据？", "确认删除", {
                    confirmButtonText: "确定", cancelButtonText: "取消", type: "warning" });
                await axios.delete("/api/tasks/" + taskId);
                ElementPlus.ElMessage.success("已删除");
                if (selectedTask.value && selectedTask.value.id === taskId) closeDetail();
                await loadTasks();
            } catch (e) { if (e !== "cancel") ElementPlus.ElMessage.error("删除失败"); }
        }

        function exportCSV(id) { window.open("/api/tasks/" + id + "/export/csv", "_blank"); }
        function exportJSON(id) { window.open("/api/tasks/" + id + "/export/json", "_blank"); }

        let _taskListTimer = null;
        function _startAutoRefresh() {
            if (_taskListTimer) return;
            _taskListTimer = setInterval(async function () {
                await loadTasks();
                if (selectedTask.value) {
                    const updated = tasks.value.find(t => t.id === selectedTask.value.id);
                    if (updated) {
                        selectedTask.value = updated;
                        await loadTaskDetail(selectedTask.value.id);
                        if (updated.status === "completed" || updated.status === "failed" || updated.status === "partial") {
                            if (wsConnection && wsConnection.readyState === WebSocket.OPEN) {
                                wsConnection.close();
                            }
                        }
                    }
                }
            }, 2000);
        }
        onMounted(function () { loadTasks(); _startAutoRefresh(); });
        onUnmounted(function () { if (wsConnection) wsConnection.close(); if (_taskListTimer) clearInterval(_taskListTimer); });

        return { newTask, submitting, tasks, taskTotal, taskPage, taskPageSize, selectedTask, taskDetail, wsProgress,
            statusTagClass, statusLabel, loadTasks, submitTask, viewTask, closeDetail, deleteTask, exportCSV, exportJSON };
    },
});

app.use(ElementPlus);
app.mount("#app");
