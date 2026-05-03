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
            const m = { pending: "tag-blue", running: "tag-yellow", processing: "tag-yellow", completed: "tag-green", failed: "tag-red" };
            return m[status] || "tag-blue";
        }
        function statusLabel(status) {
            const m = { pending: "等待中", running: "运行中", processing: "处理中", completed: "已完成", failed: "失败" };
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
            Object.assign(taskDetail, { task: null, url_records: [], up_infos: [], video_infos: [], comments: [] });
            wsProgress.percent = 0; wsProgress.message = "";
            try {
                const res = await axios.get("/api/tasks/" + task.id + "/results");
                Object.assign(taskDetail, res.data);
                wsProgress.percent = res.data.task.total_urls > 0 ? Math.round(res.data.task.completed_urls / res.data.task.total_urls * 100) : 0;
            } catch (e) { console.error(e); }
            connectWebSocket(task.id);
        }

        function connectWebSocket(taskId) {
            if (wsConnection) { wsConnection.close(); wsConnection = null; }
            if (wsHeartbeat) { clearInterval(wsHeartbeat); wsHeartbeat = null; }
            const wsUrl = (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws/tasks/" + taskId;
            wsConnection = new WebSocket(wsUrl);
            wsConnection.onmessage = function (event) {
                try {
                    const d = JSON.parse(event.data);
                    if (d.type === "pong") return;
                    if (d.type === "progress" || d.type === "complete") {
                        wsProgress.percent = d.total_urls > 0 ? Math.round(d.completed_urls / d.total_urls * 100) : 0;
                        wsProgress.message = d.message || "";
                        // 进度和消息立即更新，详情节流每秒刷新
                        const now = Date.now();
                        if (now - _lastWsUpdate > 1000) {
                            _lastWsUpdate = now;
                            loadTaskDetail(d.task_id);
                        }
                    }
                } catch (e) { console.error(e); }
            };
            wsHeartbeat = setInterval(function () {
                if (wsConnection && wsConnection.readyState === WebSocket.OPEN) wsConnection.send("ping");
            }, 30000);
            wsConnection.onclose = function () { if (wsHeartbeat) { clearInterval(wsHeartbeat); wsHeartbeat = null; } };
        }

        async function loadTaskDetail(taskId) {
            try {
                const res = await axios.get("/api/tasks/" + taskId + "/results");
                Object.assign(taskDetail, res.data);
            } catch (e) { console.error(e); }
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
        // 任务列表自动刷新：当有运行中任务时每 3 秒刷新一次
        function _startAutoRefresh() {
            if (_taskListTimer) return;
            _taskListTimer = setInterval(async function () {
                const hasRunning = tasks.value.some(t => t.status === "running" || t.status === "processing");
                if (hasRunning) {
                    await loadTasks();
                    // 如果正在查看详情且任务已完成，刷新详情
                    if (selectedTask.value) {
                        const updated = tasks.value.find(t => t.id === selectedTask.value.id);
                        if (updated && (updated.status === "completed" || updated.status === "failed")) {
                            await loadTaskDetail(selectedTask.value.id);
                            if (wsConnection && wsConnection.readyState === WebSocket.OPEN) {
                                wsConnection.close();
                            }
                        }
                    }
                }
            }, 3000);
        }
        onMounted(function () { loadTasks(); _startAutoRefresh(); });
        onUnmounted(function () { if (wsConnection) wsConnection.close(); if (_taskListTimer) clearInterval(_taskListTimer); });

        return { newTask, submitting, tasks, taskTotal, taskPage, taskPageSize, selectedTask, taskDetail, wsProgress,
            statusTagClass, statusLabel, loadTasks, submitTask, viewTask, closeDetail, deleteTask, exportCSV, exportJSON };
    },
});

app.use(ElementPlus);
app.mount("#app");
