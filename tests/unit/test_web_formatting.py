from agentkit.web.app import format_chat_response


def test_xhs_chat_response_contains_evidence_draft_and_publish_readiness():
    response = {
        "output": {
            "final": {
                "campaign_summary": "draft campaign",
                "topic": "enterprise AI agents",
                "topic_source": "tenant_default",
                "top_n": 1,
                "research_quality": {
                    "status": "limited",
                    "observed_count": 1,
                    "requested_count": 1,
                    "warnings": ["detail evidence is incomplete"],
                },
                "top_cases": [
                    {
                        "title": "Observed case",
                        "url": "https://www.xiaohongshu.com/explore/n1",
                        "author": "author",
                        "likes": 10,
                        "saves": 0,
                        "comments": 1,
                    }
                ],
                "comparison": [
                    {
                        "pattern": "Observed engagement leader",
                        "evidence": "Observed case (likes=10)",
                        "recommendation": "use the observed angle",
                    }
                ],
                "article": {"title": "Draft title", "body": "Draft body"},
                "publish": {
                    "status": "draft_created",
                    "readiness": "needs_evidence_review",
                    "review_status": "approved_with_warnings",
                    "requires_real_connector": True,
                    "media_strategy": "xhs_text_image",
                    "card_style": "涂鸦",
                },
            }
        }
    }

    text = format_chat_response(response)

    assert "### Top 案例" in text
    assert "Observed case" in text
    assert "### 对比结论" in text
    assert "detail evidence is incomplete" in text
    assert "Draft body" in text
    assert "needs_evidence_review" in text
    assert "小红书文字配图；风格：涂鸦" in text
    assert "尚未向真实小红书发布" in text
