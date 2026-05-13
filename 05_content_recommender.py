"""
AI 기반 콘텐츠 추천 엔진
Supabase + Claude API — 행동 기반 개인화 추천

설치:
    pip install anthropic supabase fastapi uvicorn python-dotenv

Supabase 테이블 SQL:
    create table contents (
      id bigint primary key generated always as identity,
      title text not null, description text, category text,
      tags text[], author text, url text,
      created_at timestamptz default now()
    );
    create table user_interactions (
      id bigint primary key generated always as identity,
      user_id text not null,
      content_id bigint references contents(id),
      action text,  -- 'view','like','share','bookmark','skip'
      duration_sec int,
      created_at timestamptz default now()
    );
    create table user_profiles (
      user_id text primary key,
      interests text[], preferred_categories text[],
      interaction_count int default 0,
      last_seen timestamptz default now()
    );
"""

import os, json
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic
from supabase import create_client, Client
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

claude    = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL", "https://your-project.supabase.co"),
    os.getenv("SUPABASE_KEY", "your-anon-key")
)
app = FastAPI(title="AI 콘텐츠 추천 엔진", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── 모델 ─────────────────────────────────────────────────────────────────────
class InteractionCreate(BaseModel):
    user_id: str
    content_id: int
    action: str  # view / like / share / bookmark / skip
    duration_sec: Optional[int] = None

class ContentCreate(BaseModel):
    title: str
    description: str
    category: str
    tags: list[str] = []
    author: str = ""
    url: str = ""

# ─── 핵심 추천 로직 ───────────────────────────────────────────────────────────
def get_user_behavior_summary(user_id: str) -> dict:
    """Supabase에서 유저 행동 데이터를 집계합니다."""
    interactions = (
        supabase.table("user_interactions")
        .select("*, contents(title, category, tags)")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
        .data
    )
    if not interactions:
        return {}
    liked     = [i for i in interactions if i["action"] in ("like","bookmark")]
    skipped   = [i for i in interactions if i["action"] == "skip"]
    viewed    = [i for i in interactions if i["action"] == "view"]
    categories = {}
    tags_all   = []
    for i in liked:
        c = i.get("contents", {})
        cat = c.get("category","")
        if cat: categories[cat] = categories.get(cat, 0) + 1
        tags_all.extend(c.get("tags",[]))
    top_categories = sorted(categories, key=categories.get, reverse=True)[:5]
    top_tags = list(dict.fromkeys(tags_all))[:10]
    skipped_cats = list({i.get("contents",{}).get("category","") for i in skipped if i.get("contents")})
    return {
        "total_interactions": len(interactions),
        "liked_count": len(liked),
        "skip_count": len(skipped),
        "avg_view_duration": sum(i.get("duration_sec",0) for i in viewed) // max(len(viewed),1),
        "top_categories": top_categories,
        "preferred_tags": top_tags,
        "disliked_categories": skipped_cats,
        "recent_titles": [i.get("contents",{}).get("title","") for i in interactions[:5] if i.get("contents")]
    }


def ai_recommend(user_id: str, available_contents: list[dict], behavior: dict, count: int = 5) -> list[dict]:
    """Claude API로 개인화 추천을 생성합니다."""
    if not available_contents:
        return []
    content_list = "\n".join([
        f"[{c['id']}] {c['title']} | {c['category']} | 태그: {', '.join(c.get('tags',[]))}"
        for c in available_contents[:30]
    ])
    behavior_str = json.dumps(behavior, ensure_ascii=False, indent=2)
    prompt = f"""당신은 개인화 콘텐츠 추천 AI입니다. 사용자 행동 데이터를 분석하여 최적의 콘텐츠를 추천하세요.

[사용자 행동 분석]
{behavior_str}

[추천 가능한 콘텐츠]
{content_list}

위 행동 데이터를 바탕으로 사용자가 가장 흥미를 가질 콘텐츠 {count}개를 선택하여 JSON으로만 응답하세요:
[
  {{
    "content_id": 숫자,
    "score": 0~100 (추천 점수),
    "reason": "추천 이유 (사용자 관심사와의 연결)",
    "personalization_note": "이 사용자에게 특별히 맞는 이유"
  }}
]
JSON 배열만 출력하세요. 다른 텍스트는 절대 포함하지 마세요."""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text
    start = text.find('[')
    end   = text.rfind(']') + 1
    recs  = json.loads(text[start:end])
    # 추천 ID로 콘텐츠 매핑
    content_map = {c['id']: c for c in available_contents}
    result = []
    for r in recs:
        c = content_map.get(r['content_id'])
        if c:
            result.append({**c, "rec_score": r['score'], "rec_reason": r['reason'],
                           "personalization_note": r['personalization_note']})
    return sorted(result, key=lambda x: x['rec_score'], reverse=True)


def ai_explain_recommendation(user_id: str, content: dict, behavior: dict) -> str:
    """특정 콘텐츠를 추천하는 이유를 자연어로 설명합니다."""
    prompt = f"""사용자에게 특정 콘텐츠를 왜 추천하는지 2~3문장으로 자연스럽게 설명해주세요.
콘텐츠: '{content['title']}' ({content['category']})
사용자가 좋아하는 카테고리: {', '.join(behavior.get('top_categories',[]))}
사용자가 관심있는 태그: {', '.join(behavior.get('preferred_tags',[])[:5])}"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

# ─── API 엔드포인트 ───────────────────────────────────────────────────────────
@app.post("/interact", summary="사용자 행동 기록")
def record_interaction(data: InteractionCreate):
    """유저의 콘텐츠 상호작용을 기록합니다."""
    result = supabase.table("user_interactions").insert({
        "user_id":      data.user_id,
        "content_id":  data.content_id,
        "action":       data.action,
        "duration_sec": data.duration_sec
    }).execute()
    # 프로필 업데이트
    supabase.table("user_profiles").upsert({
        "user_id": data.user_id,
        "last_seen": datetime.now().isoformat(),
        "interaction_count": supabase.table("user_interactions").select("id", count="exact").eq("user_id", data.user_id).execute().count
    }).execute()
    return {"success": True, "interaction_id": result.data[0]["id"]}


@app.get("/recommend/{user_id}", summary="AI 개인화 추천")
def get_recommendations(user_id: str, count: int = 5, category: Optional[str] = None):
    """사용자에게 맞춤 콘텐츠를 추천합니다."""
    # 이미 본 콘텐츠 제외
    seen_ids = {
        i["content_id"]
        for i in supabase.table("user_interactions")
            .select("content_id")
            .eq("user_id", user_id)
            .execute()
            .data
    }
    # 추천 풀 가져오기
    query = supabase.table("contents").select("*").order("created_at", desc=True).limit(50)
    if category: query = query.eq("category", category)
    all_contents = [c for c in query.execute().data if c["id"] not in seen_ids]

    if not all_contents:
        return {"recommendations": [], "message": "추천할 새 콘텐츠가 없습니다."}
    # 행동 분석
    behavior = get_user_behavior_summary(user_id)
    if not behavior:
        # 첫 방문자 — 인기 콘텐츠 반환
        popular = all_contents[:count]
        return {"recommendations": popular, "is_cold_start": True, "message": "첫 방문을 환영합니다! 인기 콘텐츠를 먼저 보여드려요."}
    # AI 추천
    recs = ai_recommend(user_id, all_contents, behavior, count)
    return {"recommendations": recs, "behavior_summary": behavior, "total_pool": len(all_contents)}


@app.get("/recommend/{user_id}/explain/{content_id}", summary="추천 이유 설명")
def explain_recommendation(user_id: str, content_id: int):
    """특정 콘텐츠 추천 이유를 AI가 설명합니다."""
    content = supabase.table("contents").select("*").eq("id", content_id).execute()
    if not content.data: raise HTTPException(404, "콘텐츠를 찾을 수 없습니다")
    behavior = get_user_behavior_summary(user_id)
    explanation = ai_explain_recommendation(user_id, content.data[0], behavior)
    return {"content_id": content_id, "explanation": explanation}


@app.get("/users/{user_id}/profile", summary="유저 프로필 + 행동 분석")
def get_user_profile(user_id: str):
    behavior = get_user_behavior_summary(user_id)
    profile  = supabase.table("user_profiles").select("*").eq("user_id", user_id).execute()
    return {"user_id": user_id, "profile": profile.data[0] if profile.data else {}, "behavior": behavior}


@app.post("/contents", summary="콘텐츠 등록")
def add_content(content: ContentCreate):
    result = supabase.table("contents").insert(content.dict()).execute()
    return result.data[0]


@app.get("/contents", summary="콘텐츠 목록")
def list_contents(category: Optional[str] = None, limit: int = 20):
    query = supabase.table("contents").select("*").order("created_at", desc=True).limit(limit)
    if category: query = query.eq("category", category)
    return query.execute().data


@app.get("/analytics/trending", summary="트렌딩 콘텐츠")
def get_trending(days: int = 7):
    """최근 N일 가장 많이 상호작용된 콘텐츠 TOP 10."""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    interactions = (
        supabase.table("user_interactions")
        .select("content_id, action, contents(title, category)")
        .gte("created_at", since)
        .in_("action", ["like","bookmark","share"])
        .execute()
        .data
    )
    counts = {}
    for i in interactions:
        cid = i["content_id"]
        if cid not in counts:
            counts[cid] = {"content_id": cid, "count": 0, "info": i.get("contents",{})}
        counts[cid]["count"] += 1
    trending = sorted(counts.values(), key=lambda x: x["count"], reverse=True)[:10]
    return {"period_days": days, "trending": trending}


if __name__ == "__main__":
    print("=== AI 콘텐츠 추천 엔진 ===")
    print("▶ 서버 시작: uvicorn content_recommender:app --reload --port 8001")
    print("▶ API 문서: http://localhost:8001/docs")
    print()
    print("주요 엔드포인트:")
    print("  POST /interact          — 행동 기록 (view/like/share/bookmark/skip)")
    print("  GET  /recommend/{uid}   — AI 개인화 추천 5개")
    print("  GET  /users/{uid}/profile — 유저 행동 분석")
    print("  GET  /analytics/trending  — 트렌딩 콘텐츠")
