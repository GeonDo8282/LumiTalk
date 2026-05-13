"""
AI 커뮤니티 보조 시스템
Supabase + Claude API 기반 게시글 자동 분류·요약·신고 감지

설치:
    pip install anthropic supabase python-dotenv fastapi uvicorn

Supabase 테이블 SQL:
    create table posts (
      id bigint primary key generated always as identity,
      title text, body text, author text,
      category text, summary text, sentiment text,
      flagged boolean default false, flag_reason text,
      created_at timestamptz default now()
    );
    create table moderation_log (
      id bigint primary key generated always as identity,
      post_id bigint references posts(id),
      action text, reason text, ai_confidence float,
      created_at timestamptz default now()
    );
"""

import os, json, asyncio
from typing import Optional
from dotenv import load_dotenv
import anthropic
from supabase import create_client, Client
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
claude  = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL", "https://your-project.supabase.co"),
    os.getenv("SUPABASE_KEY", "your-anon-key")
)
app = FastAPI(title="AI 커뮤니티 보조 시스템", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── 모델 ─────────────────────────────────────────────────────────────────────
class PostCreate(BaseModel):
    title: str
    body: str
    author: str = "anonymous"

class ModerationResult(BaseModel):
    category: str
    summary: str
    sentiment: str
    flagged: bool
    flag_reason: Optional[str]
    confidence: float
    tags: list[str]

# ─── AI 분석 함수 ─────────────────────────────────────────────────────────────
def analyze_post(title: str, body: str) -> ModerationResult:
    """Claude API로 게시글을 분석합니다."""
    prompt = f"""당신은 커뮤니티 게시판을 관리하는 AI 보조자입니다.
아래 게시글을 분석하여 JSON으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.

제목: {title}
내용: {body}

응답 JSON 형식:
{{
  "category": "자유/질문/공지/리뷰/버그신고/기술/잡담 중 하나",
  "summary": "게시글 핵심 내용 1~2문장 요약",
  "sentiment": "positive/neutral/negative 중 하나",
  "flagged": true/false,
  "flag_reason": "신고 이유 (flagged=true일 때만)",
  "confidence": 0.0~1.0 사이 신뢰도,
  "tags": ["관련태그1", "관련태그2", "관련태그3"]
}}

신고(flagged=true) 기준:
- 욕설, 혐오 표현, 차별적 언어 포함
- 스팸 또는 광고성 게시글
- 개인정보 노출 (전화번호, 주민번호 등)
- 허위 정보 또는 명백한 악의적 내용
- 성인/불법 콘텐츠"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text
    # JSON 추출
    start = text.find('{')
    end   = text.rfind('}') + 1
    data  = json.loads(text[start:end])
    return ModerationResult(**data)


def generate_reply_suggestion(post: dict) -> str:
    """AI가 게시글에 대한 답변 초안을 생성합니다."""
    prompt = f"""커뮤니티 운영자로서 아래 게시글에 공식 답변 초안을 작성해주세요.
친절하고 전문적인 어조로 2~3문장 이내로 작성하세요.

게시글 제목: {post['title']}
게시글 내용: {post['body']}
분류: {post.get('category', '기타')}"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def summarize_daily_posts(posts: list[dict]) -> str:
    """하루치 게시글들의 일간 요약을 생성합니다."""
    if not posts:
        return "오늘 게시글이 없습니다."
    posts_text = "\n".join([f"- [{p['category']}] {p['title']}: {p['summary']}" for p in posts[:20]])
    prompt = f"""오늘 커뮤니티에 올라온 게시글들을 운영자 관점에서 3~5문장으로 요약해주세요.
주요 이슈, 사용자 관심사, 필요한 운영 조치를 포함하세요.

게시글 목록:
{posts_text}"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

# ─── API 엔드포인트 ───────────────────────────────────────────────────────────
@app.post("/posts", summary="게시글 작성 + AI 자동 분석")
async def create_post(post: PostCreate, background_tasks: BackgroundTasks):
    """게시글을 저장하고 AI 분석을 백그라운드에서 실행합니다."""
    # 1. DB에 즉시 저장
    result = supabase.table("posts").insert({
        "title": post.title,
        "body": post.body,
        "author": post.author
    }).execute()
    post_id = result.data[0]["id"]

    # 2. AI 분석 (백그라운드)
    background_tasks.add_task(run_analysis, post_id, post.title, post.body)
    return {"id": post_id, "message": "게시글이 저장됐습니다. AI 분석이 진행 중입니다."}


async def run_analysis(post_id: int, title: str, body: str):
    """백그라운드 AI 분석 실행"""
    analysis = analyze_post(title, body)
    # DB 업데이트
    supabase.table("posts").update({
        "category":   analysis.category,
        "summary":    analysis.summary,
        "sentiment":  analysis.sentiment,
        "flagged":    analysis.flagged,
        "flag_reason": analysis.flag_reason
    }).eq("id", post_id).execute()
    # 신고된 경우 로그 기록
    if analysis.flagged:
        supabase.table("moderation_log").insert({
            "post_id":       post_id,
            "action":        "auto_flag",
            "reason":        analysis.flag_reason,
            "ai_confidence": analysis.confidence
        }).execute()
        print(f"⚠️  게시글 {post_id} 자동 신고 처리: {analysis.flag_reason}")


@app.get("/posts", summary="게시글 목록 조회")
def get_posts(category: Optional[str] = None, flagged: Optional[bool] = None, limit: int = 20):
    query = supabase.table("posts").select("*").order("created_at", desc=True).limit(limit)
    if category:  query = query.eq("category", category)
    if flagged is not None: query = query.eq("flagged", flagged)
    return query.execute().data


@app.get("/posts/{post_id}", summary="게시글 상세 + AI 답변 제안")
def get_post(post_id: int, suggest_reply: bool = False):
    result = supabase.table("posts").select("*").eq("id", post_id).execute()
    if not result.data: raise HTTPException(404, "게시글을 찾을 수 없습니다")
    post = result.data[0]
    if suggest_reply:
        post["suggested_reply"] = generate_reply_suggestion(post)
    return post


@app.post("/posts/{post_id}/reanalyze", summary="게시글 재분석")
def reanalyze_post(post_id: int):
    result = supabase.table("posts").select("title,body").eq("id", post_id).execute()
    if not result.data: raise HTTPException(404, "게시글을 찾을 수 없습니다")
    post = result.data[0]
    analysis = analyze_post(post["title"], post["body"])
    supabase.table("posts").update({
        "category":   analysis.category,
        "summary":    analysis.summary,
        "sentiment":  analysis.sentiment,
        "flagged":    analysis.flagged,
        "flag_reason": analysis.flag_reason
    }).eq("id", post_id).execute()
    return {"post_id": post_id, "analysis": analysis.dict()}


@app.get("/dashboard/summary", summary="운영 현황 AI 요약")
def dashboard_summary():
    """오늘의 커뮤니티 현황을 AI가 요약합니다."""
    from datetime import date
    today = str(date.today())
    posts = supabase.table("posts").select("*").gte("created_at", today).execute().data
    flagged_count = sum(1 for p in posts if p.get("flagged"))
    categories = {}
    for p in posts:
        cat = p.get("category", "미분류")
        categories[cat] = categories.get(cat, 0) + 1
    ai_summary = summarize_daily_posts(posts)
    return {
        "date": today,
        "total_posts": len(posts),
        "flagged_posts": flagged_count,
        "categories": categories,
        "ai_summary": ai_summary
    }


@app.get("/moderation/log", summary="신고 로그 조회")
def get_moderation_log(limit: int = 50):
    return supabase.table("moderation_log").select("*, posts(title,author)").order("created_at", desc=True).limit(limit).execute().data


@app.delete("/posts/{post_id}", summary="게시글 삭제 (관리자)")
def delete_post(post_id: int):
    supabase.table("posts").delete().eq("id", post_id).execute()
    return {"message": f"게시글 {post_id}가 삭제됐습니다"}


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 빠른 기능 테스트 (API 키 있을 때)
    print("=== AI 커뮤니티 보조 시스템 테스트 ===\n")
    test_posts = [
        ("Claude API 연동 질문", "안녕하세요! Claude API를 Python으로 연동하려는데 스트리밍 응답 처리가 잘 안 됩니다. 예제 코드 공유해주실 분 있나요?"),
        ("좋은 하루 되세요!", "오늘 날씨가 너무 좋네요. 다들 좋은 하루 보내세요 ☀️"),
        ("[광고] 팔로우하면 경품 추첨!", "제 인스타 팔로우하면 에어팟 드립니다! 지금 바로 팔로우! 팔로우! 팔로우!")
    ]
    for title, body in test_posts:
        print(f"제목: {title}")
        try:
            result = analyze_post(title, body)
            print(f"  분류: {result.category} | 감성: {result.sentiment} | 신고: {result.flagged}")
            print(f"  요약: {result.summary}")
            if result.flagged: print(f"  ⚠️  신고 이유: {result.flag_reason}")
        except Exception as e:
            print(f"  ❌ 오류: {e}")
        print()

    print("\n▶ FastAPI 서버 시작: uvicorn community_ai:app --reload")
    print("▶ API 문서: http://localhost:8000/docs")
