from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import json
import uuid
import re
from datetime import datetime
from threading import Lock

# =====================================================
# App Init 與動態路徑自動偵測（完美適應雲端環境）
# =====================================================
app = Flask(__name__)
CORS(app)

# 🔒 自動抓取當前執行目錄，解除 Windows 絕對路徑限制
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# 系統設定檔保留在 data 根目錄
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PROFILE_FILE = os.path.join(DATA_DIR, "profile.json")
TWEETS_FILE = os.path.join(DATA_DIR, "tweets_cache.json")

# 🌟 實體資料唯一讀寫目錄：精準鎖定 combined_data
COMBINED_DATA_DIR = os.path.join(DATA_DIR, "combined_data")

# 自動確保目錄存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(COMBINED_DATA_DIR, exist_ok=True)

# 統一的執行緒鎖，保護檔案讀寫與全域快取安全
file_lock = Lock()

# 全域動態索引緩存：比對 [名稱] -> [原始檔案路徑]
friend_index = {}

# =====================================================
# Utils 工具組
# =====================================================
def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name or "anonymous")

def calculate_age(birthday_str: str):
    try:
        birth = datetime.strptime(birthday_str, "%Y-%m-%d")
        today = datetime.today()
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    except Exception as e:
        print(f"⚠️ 年齡計算失敗 ({birthday_str}): {e}")
        return None

# =====================================================
# Cognition Engine & Association Memory (認知與關聯記憶引擎)
# =====================================================
class CognitionManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.state = {"total_interactions": 0, "last_sync": None, "mood_index": 0.5}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
            except Exception as e:
                print(f"⚠️ 載入認知狀態失敗: {e}")

    def flush(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"⚠️ 寫入認知狀態失敗: {e}")

    def update_from_feedback(self, reward: float):
        self.state["total_interactions"] += 1
        alpha = 0.1
        self.state["mood_index"] = (1 - alpha) * self.state["mood_index"] + alpha * reward
        self.state["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class AssociationMemory:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.matrix = {}
        self.load_all_links()

    def load_all_links(self):
        if not os.path.exists(self.data_dir):
            return
        # 🌟 僅掃描 combined_data 內的實體
        for root, _, files in os.walk(self.data_dir):
            for file in files:
                if file.endswith(".json"):
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                            d = json.load(f)
                            name = d.get("name")
                            rels = d.get("matrix_relations", {})
                            if name:
                                self.matrix[name] = rels
                    except Exception as e:
                        print(f"⚠️ 載入關聯記憶鏈接失敗 ({file}): {e}")

    def strengthen_link(self, source, target, weight=0.1):
        if source not in self.matrix: self.matrix[source] = {}
        if target not in self.matrix: self.matrix[target] = {}
        
        current = self.matrix[source].get(target, [])
        if "關聯共振" not in current:
            current.append("關聯共振")
        self.matrix[source][target] = current

cog_engine = CognitionManager(STATE_FILE)
assoc_engine = AssociationMemory(COMBINED_DATA_DIR)

def rebuild_friend_index():
    """
    執行緒安全的索引重建
    優化邏輯：先在局部變數建立索引，最後一刻才寫入全域變數，最大程度減少鎖定時間
    """
    global friend_index
    new_index = {}
    if os.path.exists(COMBINED_DATA_DIR):
        for root, _, files in os.walk(COMBINED_DATA_DIR):
            for file in files:
                if file.endswith(".json"):
                    try:
                        path = os.path.join(root, file)
                        with open(path, 'r', encoding='utf-8') as f:
                            d = json.load(f)
                            name = d.get("name")
                            if name:
                                new_index[name] = path
                    except Exception as e:
                        print(f"⚠️ 重建索引讀取失敗 ({file}): {e}")
                        
    with file_lock:
        friend_index = new_index

# 初始化時建置一次索引
rebuild_friend_index()

# =====================================================
# API 路由控制層
# =====================================================

@app.route("/api/profile", methods=["GET", "POST"])
def manage_profile():
    if request.method == "GET":
        with file_lock:
            if os.path.exists(PROFILE_FILE):
                try:
                    with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                        return jsonify(json.load(f))
                except Exception as e:
                    print(f"⚠️ 讀取 Profile 失敗: {e}")
            return jsonify({"basic_info": {}})
    else:
        data = request.get_json(silent=True) or {}
        with file_lock:
            try:
                with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"error": f"Save profile failed: {str(e)}"}), 500

@app.route("/api/friends", methods=["GET"])
def get_all_friends():
    # ⚡ 效能優化：不再呼叫 rebuild_friend_index()，直接利用記憶體快取
    results = []
    with file_lock:
        # 複製一份快取項目，避免迴圈中被其他執行緒修改拋出 RuntimeError
        current_items = list(friend_index.items())
        
    for name, path in current_items:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    results.append(json.load(f))
            except Exception as e:
                print(f"⚠️ 讀取朋友資料檔案失敗 ({name}): {e}")
    return jsonify(results)

@app.route("/api/friends/<name>", methods=["GET"])
def get_single_friend(name):
    # ⚡ 效能優化：直接從記憶體快取獲取路徑
    with file_lock:
        path = friend_index.get(name)
        if not path or not os.path.exists(path):
            return jsonify({"error": "not found"}), 404
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": f"Read friend data failed: {str(e)}"}), 500

@app.route("/api/friends", methods=["POST"])
@app.route("/api/friends/<name>", methods=["PUT", "DELETE"])
def handle_friend_matrix(name=None):
    global friend_index
    
    if request.method in ["POST", "PUT"]:
        data = request.get_json(silent=True) or {}
        actual_name = data.get("name")
        if not actual_name:
            return jsonify({"error": "Missing 'name' attribute"}), 400

        server_node = safe_filename(data.get("server_node", "default"))
        filename = f"{safe_filename(actual_name)}.json"
        
        # 🌟 統一並鎖定寫入路徑
        node_dir = os.path.join(COMBINED_DATA_DIR, server_node)
        os.makedirs(node_dir, exist_ok=True)
        path = os.path.join(node_dir, filename)

        with file_lock:
            # 處理生日與年齡換算
            if data.get("birthday"):
                data["age"] = calculate_age(data["birthday"])
            if not data.get("friend_id"):
                data["friend_id"] = str(uuid.uuid4())[:8]

            # 若為修改(PUT)且「名稱」或「伺服器節點」有變更導致路徑不同，刪除舊檔案並清理舊快取
            if request.method == "PUT" and name:
                old_path = friend_index.get(name)
                if old_path and os.path.exists(old_path) and old_path != path:
                    try:
                        os.remove(old_path)
                    except Exception as e:
                        print(f"⚠️ 移除舊檔案失敗: {e}")
                if actual_name != name:
                    friend_index.pop(name, None)

            # 單一精準寫入檔案
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                # ⚡ 增量更新全域快取，確保當前執行緒與其餘請求即時同步
                friend_index[actual_name] = path
            except Exception as e:
                return jsonify({"error": f"Write friend data failed: {str(e)}"}), 500

            # 激發記憶引擎交織關聯 (置於鎖內保護 assoc_engine.matrix)
            if data.get("matrix_relations") and isinstance(data["matrix_relations"], dict):
                for target, tags in data["matrix_relations"].items():
                    weight = len(tags) * 0.1 if isinstance(tags, list) else 0.2
                    assoc_engine.strengthen_link(actual_name, target, weight)

        return jsonify({"status": "ok", "friend_id": data["friend_id"], "path": path})

    elif request.method == "DELETE":
        with file_lock:
            path = friend_index.get(name)
            if not path:
                return jsonify({"error": "not found"}), 404
            try:
                # 刪除指定的單一檔案
                if os.path.exists(path):
                    os.remove(path)
                # ⚡ 增量移除快取項目
                friend_index.pop(name, None)
                return jsonify({"status": "deleted"})
            except Exception as e:
                return jsonify({"error": f"delete failed: {str(e)}"}), 500

@app.route("/api/learn", methods=["POST"])
def learn():
    data = request.get_json(silent=True) or {}
    try:
        reward = float(data.get("reward", 0.5))
    except (ValueError, TypeError):
        reward = 0.5
        
    with file_lock:
        cog_engine.update_from_feedback(reward)
        cog_engine.flush()
        # 複製一份 state 避免在 jsonify 時發生執行緒讀寫衝突
        current_state = cog_engine.state.copy()
        
    return jsonify({"status": "learned", "reward": reward, "state": current_state})

@app.route("/api/tweets", methods=["GET", "POST"])
def handle_tweets():
    if request.method == "GET":
        with file_lock:
            if os.path.exists(TWEETS_FILE):
                try:
                    with open(TWEETS_FILE, 'r', encoding='utf-8') as f:
                        return jsonify(json.load(f))
                except Exception as e:
                    print(f"⚠️ 讀取推文快取失敗: {e}")
            return jsonify([])
    else:
        tweets = request.get_json(silent=True) or []
        with file_lock:
            try:
                with open(TWEETS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(tweets, f, ensure_ascii=False, indent=4)
                return jsonify({"status": "synced", "count": len(tweets)})
            except Exception as e:
                return jsonify({"error": f"Sync tweets failed: {str(e)}"}), 500

@app.route("/api/sync", methods=["POST"])
def full_sync():
    # 強制執行全盤實體硬碟掃描與索引校正
    rebuild_friend_index()
    
    with file_lock:
        cog_engine.state["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cog_engine.flush()
        current_state = cog_engine.state.copy()
        active_nodes_count = len(friend_index)
        
    return jsonify({"status": "synchronized", "matrix_state": current_state, "active_nodes": active_nodes_count})

# =====================================================
# 🌐 靜態網頁路由（新增：使雲端伺服器能直接讀取前端 HTML 畫面）
# =====================================================
@app.route('/')
def serve_index():
    """預設首頁，自動讀取同目錄下的 index.html"""
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def serve_static_files(filename):
    """自動讀取其他的 html, css, js 檔案"""
    # 避免攔截並破壞原本的 /api/ 路由
    if filename.startswith('api/'):
        return jsonify({"error": "Not Found"}), 404
    return send_from_directory('.', filename)

if __name__ == "__main__":
    # 啟動後端本地服務網
    print(f"🚀 Irio 核心通訊矩陣已開機")
    print(f"📁 設定檔存放目錄: {DATA_DIR}")
    print(f"🌸 實體矩陣唯一鎖定路徑: {COMBINED_DATA_DIR}\\[伺服器節點]\\[名稱].json")
    
    # 讀取雲端環境變數中的 Port（Render 會自動指派），若本地執行則預設 5000
    port = int(os.environ.get("PORT", 5000))
    # 將 host 改為 0.0.0.0 以允許外部雲端連線
    app.run(host="0.0.0.0", port=port, debug=False)