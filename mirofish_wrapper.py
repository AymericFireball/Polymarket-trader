"""
Stage 4: MiroFish Simulation Wrapper
======================================
Interfaces with MiroFish via its HTTP API (runs as Docker container).

SETUP:
1. cd mirofish-docker/
2. Edit .env with your LLM API key and Zep API key
3. docker compose up -d
4. MiroFish runs at http://localhost:5001 (backend) / http://localhost:3000 (UI)

MiroFish API Lifecycle:
  1. POST /api/graph/ontology/generate  — Upload files + requirement → project_id
  2. POST /api/graph/build              — Build Zep knowledge graph → task_id (async)
  3. POST /api/simulation/create        — Create simulation from project → simulation_id
  4. POST /api/simulation/prepare       — LLM generates agent profiles + config → task_id (async)
  5. POST /api/simulation/start         — Run the simulation
  6. GET  /api/simulation/<id>/run-status — Poll progress
  7. POST /api/simulation/interview/all — Interview all agents with a question
  8. POST /api/report/generate          — Generate analysis report → task_id (async)
  9. GET  /api/report/by-simulation/<id> — Retrieve report
"""

import os
import sys
import json
import time
import re
import requests
import tempfile
from datetime import datetime, timezone
from typing import Dict, Optional, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_MIROFISH_URL = "http://localhost:5001"


def _get_mirofish_url() -> str:
    """Get MiroFish API base URL."""
    try:
        from config import MIROFISH_API_URL
        if MIROFISH_API_URL:
            return MIROFISH_API_URL.rstrip("/")
    except (ImportError, AttributeError):
        pass
    return os.environ.get("MIROFISH_API_URL", DEFAULT_MIROFISH_URL)


class MiroFishClient:
    """HTTP client for MiroFish's backend API.

    Implements the full simulation lifecycle:
      ontology → graph build → create sim → prepare → start → monitor → interview → report
    """

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or _get_mirofish_url()).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # ─── Health ────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Check if MiroFish backend is running."""
        try:
            resp = self.session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except requests.exceptions.ConnectionError:
            return False

    # ─── Graph / Project ───────────────────────────────────────────────
    def create_project_with_text(
        self,
        simulation_requirement: str,
        context_text: str,
        project_name: str = "Polymarket Prediction",
    ) -> Optional[str]:
        """Step 1: Upload context text and generate ontology → returns project_id.

        Uses multipart/form-data with a temporary .txt file containing the context.
        """
        try:
            # Write context to a temp file for upload
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="mirofish_") as f:
                f.write(context_text)
                tmp_path = f.name

            with open(tmp_path, "rb") as fh:
                files = {"files": ("context.txt", fh, "text/plain")}
                data = {
                    "simulation_requirement": simulation_requirement,
                    "project_name": project_name,
                }
                resp = self.session.post(
                    f"{self.base_url}/api/graph/ontology/generate",
                    files=files,
                    data=data,
                    timeout=120,
                )

            os.unlink(tmp_path)

            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    return result.get("data", {}).get("project_id")
            print(f"  [MiroFish] Create project failed: {resp.status_code} {resp.text[:300]}")
            return None
        except Exception as e:
            print(f"  [MiroFish] Create project error: {e}")
            return None

    def build_graph(self, project_id: str) -> Optional[str]:
        """Step 2: Build knowledge graph from uploaded content → returns task_id."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/graph/build",
                json={"project_id": project_id},
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    return result.get("data", {}).get("task_id")
            print(f"  [MiroFish] Build graph failed: {resp.status_code} {resp.text[:300]}")
            return None
        except Exception as e:
            print(f"  [MiroFish] Build graph error: {e}")
            return None

    def wait_for_task(self, task_id: str, timeout_seconds: int = 300, poll_interval: int = 5) -> Dict:
        """Poll a task until it completes or times out."""
        start = time.time()
        while time.time() - start < timeout_seconds:
            try:
                resp = self.session.get(f"{self.base_url}/api/graph/task/{task_id}", timeout=15)
                if resp.status_code == 200:
                    result = resp.json()
                    data = result.get("data", result)
                    status = data.get("status", "").lower()
                    if status in ("completed", "done", "success", "failed", "error"):
                        return data
                    print(f"  [MiroFish] Task {task_id}: {status} - {data.get('progress', '?')}%")
            except Exception as e:
                print(f"  [MiroFish] Task poll error: {e}")
            time.sleep(poll_interval)
        return {"status": "timeout", "task_id": task_id}

    def get_project(self, project_id: str) -> Optional[Dict]:
        """Get project details including graph_id."""
        try:
            resp = self.session.get(f"{self.base_url}/api/graph/project/{project_id}", timeout=15)
            if resp.status_code == 200:
                result = resp.json()
                return result.get("data", result)
            return None
        except Exception as e:
            print(f"  [MiroFish] Get project error: {e}")
            return None

    # ─── Simulation Lifecycle ──────────────────────────────────────────
    def create_simulation(
        self,
        project_id: str,
        graph_id: str = None,
        enable_twitter: bool = True,
        enable_reddit: bool = True,
    ) -> Optional[str]:
        """Step 3: Create a new simulation → returns simulation_id."""
        payload = {
            "project_id": project_id,
            "enable_twitter": enable_twitter,
            "enable_reddit": enable_reddit,
        }
        if graph_id:
            payload["graph_id"] = graph_id
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/create",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    return result.get("data", {}).get("simulation_id")
            print(f"  [MiroFish] Create simulation failed: {resp.status_code} {resp.text[:300]}")
            return None
        except Exception as e:
            print(f"  [MiroFish] Create simulation error: {e}")
            return None

    def prepare_simulation(
        self,
        simulation_id: str,
        entity_types: List[str] = None,
        use_llm_for_profiles: bool = True,
        parallel_profile_count: int = 5,
        force_regenerate: bool = False,
    ) -> Optional[str]:
        """Step 4: Prepare simulation — LLM generates agent profiles + config → returns task_id."""
        payload = {
            "simulation_id": simulation_id,
            "use_llm_for_profiles": use_llm_for_profiles,
            "parallel_profile_count": parallel_profile_count,
            "force_regenerate": force_regenerate,
        }
        if entity_types:
            payload["entity_types"] = entity_types
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/prepare",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    data = result.get("data", {})
                    # If already prepared, return special marker
                    if data.get("already_prepared"):
                        return "ALREADY_PREPARED"
                    return data.get("task_id")
            print(f"  [MiroFish] Prepare simulation failed: {resp.status_code} {resp.text[:300]}")
            return None
        except Exception as e:
            print(f"  [MiroFish] Prepare simulation error: {e}")
            return None

    def check_prepare_status(self, task_id: str = None, simulation_id: str = None) -> Dict:
        """Check the status of a prepare task."""
        payload = {}
        if task_id and task_id != "ALREADY_PREPARED":
            payload["task_id"] = task_id
        if simulation_id:
            payload["simulation_id"] = simulation_id
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/prepare/status",
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"status": "error", "message": resp.text[:300]}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def wait_for_prepare(self, task_id: str, simulation_id: str = None, timeout_seconds: int = 600, poll_interval: int = 8) -> Dict:
        """Wait for simulation preparation to complete."""
        if task_id == "ALREADY_PREPARED":
            return {"status": "completed", "already_prepared": True}
        start = time.time()
        while time.time() - start < timeout_seconds:
            status_data = self.check_prepare_status(task_id=task_id, simulation_id=simulation_id)
            status = status_data.get("status", "").lower()
            progress = status_data.get("progress", "?")
            print(f"  [MiroFish] Prepare: {status} ({progress}%) - {status_data.get('message', '')[:80]}")
            if status in ("completed", "ready"):
                return status_data
            if status in ("failed", "error"):
                return status_data
            if status_data.get("already_prepared"):
                return status_data
            time.sleep(poll_interval)
        return {"status": "timeout"}

    def start_simulation(
        self,
        simulation_id: str,
        platform: str = "parallel",
        max_rounds: int = None,
        force: bool = False,
    ) -> Dict:
        """Step 5: Start running the simulation."""
        payload = {
            "simulation_id": simulation_id,
            "platform": platform,
            "force": force,
        }
        if max_rounds:
            payload["max_rounds"] = max_rounds
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/start",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                return result.get("data", result)
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_run_status(self, simulation_id: str) -> Dict:
        """Step 6: Check simulation run progress."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/simulation/{simulation_id}/run-status",
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"runner_status": "unknown", "error": resp.text[:300]}
        except Exception as e:
            return {"runner_status": "error", "error": str(e)}

    def wait_for_simulation(self, simulation_id: str, timeout_seconds: int = 1800, poll_interval: int = 15) -> Dict:
        """Poll simulation run-status until complete or timeout."""
        start = time.time()
        while time.time() - start < timeout_seconds:
            status = self.get_run_status(simulation_id)
            runner = status.get("runner_status", "unknown").lower()
            current = status.get("current_round", "?")
            total = status.get("total_rounds", "?")
            pct = status.get("progress_percent", "?")
            print(f"  [MiroFish] Sim: {runner} — round {current}/{total} ({pct}%)")
            if runner in ("completed", "stopped", "error", "failed"):
                return status
            time.sleep(poll_interval)
        return {"runner_status": "timeout", "simulation_id": simulation_id}

    def stop_simulation(self, simulation_id: str) -> Dict:
        """Stop a running simulation."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/stop",
                json={"simulation_id": simulation_id},
                timeout=15,
            )
            return resp.json() if resp.status_code == 200 else {"error": resp.text[:300]}
        except Exception as e:
            return {"error": str(e)}

    # ─── Interview Agents ──────────────────────────────────────────────
    def interview_single(self, simulation_id: str, agent_id: int, prompt: str, platform: str = None, timeout: int = 60) -> Dict:
        """Interview a single agent."""
        payload = {
            "simulation_id": simulation_id,
            "agent_id": agent_id,
            "prompt": prompt,
            "timeout": timeout,
        }
        if platform:
            payload["platform"] = platform
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/interview",
                json=payload,
                timeout=timeout + 10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"error": str(e)}

    def interview_all(self, simulation_id: str, prompt: str, platform: str = None, timeout: int = 300) -> Dict:
        """Interview ALL agents with the same question — key for prediction aggregation."""
        payload = {
            "simulation_id": simulation_id,
            "prompt": prompt,
            "timeout": timeout,
        }
        if platform:
            payload["platform"] = platform
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/interview/all",
                json=payload,
                timeout=timeout + 30,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"error": str(e)}

    def get_interview_history(self, simulation_id: str, platform: str = None, limit: int = 100) -> Dict:
        """Get all interview responses from the database."""
        payload = {"simulation_id": simulation_id, "limit": limit}
        if platform:
            payload["platform"] = platform
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/interview/history",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"error": resp.text[:300]}
        except Exception as e:
            return {"error": str(e)}

    # ─── Environment Status ────────────────────────────────────────────
    def check_env_alive(self, simulation_id: str) -> Dict:
        """Check if simulation environment is alive (can receive interviews)."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/env-status",
                json={"simulation_id": simulation_id},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"env_alive": False}
        except Exception:
            return {"env_alive": False}

    def close_env(self, simulation_id: str) -> Dict:
        """Gracefully close simulation environment."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/simulation/close-env",
                json={"simulation_id": simulation_id},
                timeout=60,
            )
            return resp.json() if resp.status_code == 200 else {"error": resp.text[:300]}
        except Exception as e:
            return {"error": str(e)}

    # ─── Data Queries ──────────────────────────────────────────────────
    def get_posts(self, simulation_id: str, platform: str = "reddit", limit: int = 50) -> Dict:
        """Get posts generated during simulation."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/simulation/{simulation_id}/posts",
                params={"platform": platform, "limit": limit},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"posts": [], "error": resp.text[:300]}
        except Exception as e:
            return {"posts": [], "error": str(e)}

    def get_agent_stats(self, simulation_id: str) -> Dict:
        """Get per-agent statistics."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/simulation/{simulation_id}/agent-stats",
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"stats": []}
        except Exception as e:
            return {"stats": [], "error": str(e)}

    # ─── Reports ───────────────────────────────────────────────────────
    def generate_report(self, simulation_id: str, force_regenerate: bool = False) -> Optional[str]:
        """Generate analysis report (async) → returns task_id."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/report/generate",
                json={"simulation_id": simulation_id, "force_regenerate": force_regenerate},
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    return result.get("data", {}).get("task_id")
            print(f"  [MiroFish] Generate report failed: {resp.status_code} {resp.text[:300]}")
            return None
        except Exception as e:
            print(f"  [MiroFish] Generate report error: {e}")
            return None

    def get_report_by_simulation(self, simulation_id: str) -> Dict:
        """Get the generated report for a simulation."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/report/by-simulation/{simulation_id}",
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def check_report_status(self, task_id: str) -> Dict:
        """Check report generation progress."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/report/generate/status",
                json={"task_id": task_id},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {"status": "error"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# High-level prediction pipeline
# ═══════════════════════════════════════════════════════════════════════

def run_mirofish_prediction(
    market: Dict,
    context_text: str,
    market_type: str = "other",
    num_rounds: int = None,
    platform: str = "reddit",
    timeout_seconds: int = 1800,
) -> Dict:
    """
    End-to-end MiroFish prediction for a Polymarket question.

    Flow:
      1. Health check
      2. Create project (upload context → ontology)
      3. Build knowledge graph
      4. Create simulation
      5. Prepare (LLM generates agent profiles)
      6. Run simulation
      7. Interview all agents with the prediction question
      8. Parse responses into a probability estimate

    Args:
        market: Dict with at least 'question' key
        context_text: Background info for agents to consider
        market_type: Category hint (not used for agent config — MiroFish generates its own)
        num_rounds: Max simulation rounds (None = let MiroFish decide)
        platform: 'reddit', 'twitter', or 'parallel'
        timeout_seconds: Max time to wait for simulation

    Returns:
        Dict with sim_probability, confidence, agent_responses, etc.
    """
    question = market.get("question", "Unknown question")
    started_at = datetime.now(timezone.utc).isoformat()

    client = MiroFishClient()

    # Step 0: Health check
    if not client.health_check():
        return {
            "status": "not_running",
            "sim_probability": None,
            "error": f"MiroFish not running at {client.base_url}. Start: cd mirofish-docker && docker compose up -d",
            "ran_at": started_at,
        }
    print(f"  [MiroFish] Connected to {client.base_url}")

    # Step 1: Create project with context
    print(f"  [MiroFish] Creating project for: {question[:80]}...")
    project_id = client.create_project_with_text(
        simulation_requirement=f"Predict the outcome of: {question}\n\nContext:\n{context_text[:2000]}",
        context_text=context_text,
        project_name=f"Polymarket: {question[:60]}",
    )
    if not project_id:
        return {"status": "error", "sim_probability": None, "error": "Failed to create project/ontology", "ran_at": started_at}
    print(f"  [MiroFish] Project created: {project_id}")

    # Step 2: Build knowledge graph
    print(f"  [MiroFish] Building knowledge graph...")
    task_id = client.build_graph(project_id)
    if task_id:
        task_result = client.wait_for_task(task_id, timeout_seconds=300)
        if task_result.get("status") in ("failed", "error", "timeout"):
            print(f"  [MiroFish] Graph build warning: {task_result.get('status')} - continuing anyway")
    else:
        print(f"  [MiroFish] Graph build skipped or failed - continuing anyway")

    # Step 3: Create simulation
    print(f"  [MiroFish] Creating simulation...")
    project_data = client.get_project(project_id)
    graph_id = project_data.get("graph_id") if project_data else None

    simulation_id = client.create_simulation(
        project_id=project_id,
        graph_id=graph_id,
        enable_twitter=(platform in ("twitter", "parallel")),
        enable_reddit=(platform in ("reddit", "parallel")),
    )
    if not simulation_id:
        return {"status": "error", "sim_probability": None, "error": "Failed to create simulation", "ran_at": started_at}
    print(f"  [MiroFish] Simulation created: {simulation_id}")

    # Step 4: Prepare simulation (LLM generates profiles)
    print(f"  [MiroFish] Preparing simulation (LLM generating agent profiles)...")
    prep_task_id = client.prepare_simulation(simulation_id)
    if not prep_task_id:
        return {"status": "error", "sim_probability": None, "error": "Failed to start preparation", "ran_at": started_at}

    prep_result = client.wait_for_prepare(prep_task_id, simulation_id=simulation_id, timeout_seconds=600)
    if prep_result.get("status") in ("failed", "error", "timeout"):
        return {"status": "error", "sim_probability": None, "error": f"Preparation failed: {prep_result}", "ran_at": started_at}
    print(f"  [MiroFish] Preparation complete!")

    # Step 5: Start simulation
    print(f"  [MiroFish] Starting simulation (platform={platform})...")
    start_result = client.start_simulation(
        simulation_id=simulation_id,
        platform=platform,
        max_rounds=num_rounds,
    )
    if not start_result.get("simulation_id") and not start_result.get("runner_status"):
        return {"status": "error", "sim_probability": None, "error": f"Failed to start: {start_result}", "ran_at": started_at}

    # Step 6: Wait for simulation to complete
    print(f"  [MiroFish] Simulation running... (timeout: {timeout_seconds}s)")
    sim_result = client.wait_for_simulation(simulation_id, timeout_seconds=timeout_seconds)
    runner_status = sim_result.get("runner_status", "unknown")
    print(f"  [MiroFish] Simulation finished: {runner_status}")

    # Step 7: Interview all agents for prediction
    print(f"  [MiroFish] Interviewing all agents...")
    interview_prompt = (
        f"Based on everything you've discussed and observed, what is the probability (0-100%) "
        f"that the following will happen? Give ONLY a number between 0 and 100, then a brief "
        f"one-sentence explanation.\n\nQuestion: {question}"
    )

    interview_result = {}
    env_status = client.check_env_alive(simulation_id)
    if env_status.get("env_alive"):
        interview_result = client.interview_all(
            simulation_id=simulation_id,
            prompt=interview_prompt,
            platform=platform if platform != "parallel" else None,
            timeout=300,
        )
    else:
        print(f"  [MiroFish] Env not alive for interviews, skipping")

    # Step 8: Parse interview responses into probability
    parsed = parse_interview_responses(interview_result, question)

    # Also try to get the report
    posts = client.get_posts(simulation_id, platform=platform if platform != "parallel" else "reddit")

    # Cleanup: close environment
    client.close_env(simulation_id)

    return {
        "status": "success" if parsed["sim_probability"] is not None else "partial",
        "sim_probability": parsed["sim_probability"],
        "confidence": parsed["confidence"],
        "agent_count": parsed["agent_count"],
        "responses_parsed": parsed["responses_parsed"],
        "key_drivers": parsed.get("sample_reasons", [])[:5],
        "dissent_flag": parsed.get("spread", 0) > 0.25,
        "simulation_id": simulation_id,
        "project_id": project_id,
        "runner_status": runner_status,
        "total_posts": posts.get("total", 0),
        "market_question": question,
        "ran_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_interview_responses(interview_data: Dict, question: str = "") -> Dict:
    """
    Parse agent interview responses to extract probability estimates.

    Looks for numbers 0-100 in each agent's response and aggregates them.
    """
    results = interview_data.get("result", {}).get("results", {})
    if not results:
        return {
            "sim_probability": None,
            "confidence": "none",
            "agent_count": 0,
            "responses_parsed": 0,
            "spread": None,
            "sample_reasons": [],
        }

    probabilities = []
    reasons = []

    for key, response_data in results.items():
        response_text = response_data.get("response", "")
        if not response_text:
            continue

        # Extract number 0-100 from response
        numbers = re.findall(r'\b(\d{1,3})(?:\s*%|\b)', response_text)
        for num_str in numbers:
            num = int(num_str)
            if 0 <= num <= 100:
                probabilities.append(num / 100.0)
                # Get the explanation part
                reason = response_text.strip()
                if len(reason) > 10:
                    reasons.append(reason[:200])
                break

    if not probabilities:
        return {
            "sim_probability": None,
            "confidence": "none",
            "agent_count": len(results),
            "responses_parsed": 0,
            "spread": None,
            "sample_reasons": reasons[:5],
        }

    avg_prob = sum(probabilities) / len(probabilities)

    # Calculate spread (standard deviation)
    if len(probabilities) > 1:
        variance = sum((p - avg_prob) ** 2 for p in probabilities) / len(probabilities)
        spread = variance ** 0.5
    else:
        spread = 0.5  # unknown

    confidence = "high" if spread < 0.15 else "medium" if spread < 0.30 else "low"

    return {
        "sim_probability": round(avg_prob, 4),
        "confidence": confidence,
        "agent_count": len(results),
        "responses_parsed": len(probabilities),
        "spread": round(spread, 4),
        "sample_reasons": reasons[:5],
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI test
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    client = MiroFishClient()
    print(f"MiroFish API URL: {client.base_url}")
    print(f"Health check: {'OK' if client.health_check() else 'FAILED'}")

    if client.health_check():
        print("\nQuick API test:")
        # List existing simulations
        try:
            resp = client.session.get(f"{client.base_url}/api/simulation/list", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                sims = data.get("data", {}).get("simulations", [])
                print(f"  Existing simulations: {len(sims)}")
                for s in sims[:3]:
                    print(f"    - {s.get('simulation_id')}: {s.get('status')}")
            else:
                print(f"  List simulations: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  List simulations error: {e}")

        # List existing projects
        try:
            resp = client.session.get(f"{client.base_url}/api/graph/project/list", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                projects = data.get("data", {}).get("projects", [])
                print(f"  Existing projects: {len(projects)}")
                for p in projects[:3]:
                    print(f"    - {p.get('project_id')}: {p.get('name', 'unnamed')}")
            else:
                print(f"  List projects: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  List projects error: {e}")

    print("\nWrapper ready. Use run_mirofish_prediction(market, context) for full pipeline.")
