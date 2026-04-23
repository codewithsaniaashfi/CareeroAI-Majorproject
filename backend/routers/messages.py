
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from auth import require_role, get_current_user
from database import db
from typing import List, Dict, Any
from chat_service import chat_service
import uuid
from datetime import datetime

router = APIRouter(prefix="/messages", tags=["messages"])


class SendMessageRequest(BaseModel):
    recipient_id: str
    content: str
    application_id: str = None


class ChatMessageRequest(BaseModel):
    message: str
    conversation_history: List[Dict[str, str]] = []


@router.get("/inbox")
async def get_inbox(current_user: Dict = Depends(get_current_user)):
    """Get user's inbox - latest message from each conversation"""
    try:
        user_id = current_user["user_id"]
        
        # Get all messages where user is recipient or sender
        messages = (
            db.get_client()
            .table("messages")
            .select("*, sender:sender_id(email, role)")
            .or_(f"sender_id.eq.{user_id},recipient_id.eq.{user_id}")
            .order("created_at", desc=True)
            .execute()
        )
        
        # Group by conversation partner
        conversations = {}
        for msg in messages.data:
            # Determine conversation partner
            partner_id = msg["sender_id"] if msg["recipient_id"] == user_id else msg["recipient_id"]
            
            # Only keep latest message per partner
            if partner_id not in conversations:
                # Get partner info
                partner = db.get_client().table("users").select("email, role").eq("id", partner_id).single().execute()
                
                conversations[partner_id] = {
                    "id": msg["id"],
                    "sender_id": msg["sender_id"],
                    "recipient_id": msg["recipient_id"],
                    "content": msg["content"],
                    "read": msg["read"],
                    "created_at": msg["created_at"],
                    "sender_name": partner.data["email"].split("@")[0] if partner.data else "Unknown",
                    "sender_role": partner.data["role"] if partner.data else "unknown"
                }
        
        return list(conversations.values())
        
    except Exception as e:
        print(f"Inbox error: {e}")
        raise HTTPException(500, f"Failed to fetch inbox: {str(e)}")


@router.get("/conversation/{user_id}")
async def get_conversation(user_id: str, current_user: Dict = Depends(get_current_user)):
    """Get conversation with specific user"""
    try:
        current_user_id = current_user["user_id"]
        
        # Get all messages between the two users
        messages = (
            db.get_client()
            .table("messages")
            .select("*")
            .or_(
                f"and(sender_id.eq.{current_user_id},recipient_id.eq.{user_id}),"
                f"and(sender_id.eq.{user_id},recipient_id.eq.{current_user_id})"
            )
            .order("created_at")
            .execute()
        )
        
        # Mark messages as read
        db.get_client().table("messages").update({"read": True}).eq("recipient_id", current_user_id).eq("sender_id", user_id).execute()
        
        return {"messages": messages.data}
        
    except Exception as e:
        print(f"Conversation error: {e}")
        raise HTTPException(500, f"Failed to fetch conversation: {str(e)}")


@router.get("/unread-count")
async def get_unread_count(current_user: Dict = Depends(get_current_user)):
    """Get count of unread messages"""
    try:
        user_id = current_user["user_id"]
        
        result = (
            db.get_client()
            .table("messages")
            .select("id", count="exact")
            .eq("recipient_id", user_id)
            .eq("read", False)
            .execute()
        )
        
        return {"unread_count": result.count or 0}
        
    except Exception as e:
        print(f"Unread count error: {e}")
        raise HTTPException(500, f"Failed to fetch unread count: {str(e)}")


@router.post("/send")
async def send_message(data: SendMessageRequest, current_user: Dict = Depends(get_current_user)):
    """Send a message"""
    try:
        user_id = current_user["user_id"]
        
        # Create message
        message = {
            "id": str(uuid.uuid4()),
            "sender_id": user_id,
            "recipient_id": data.recipient_id,
            "content": data.content,
            "read": False,
            "created_at": datetime.utcnow().isoformat()
        }
        
        if data.application_id:
            message["application_id"] = data.application_id
        
        result = db.get_client().table("messages").insert(message).execute()
        
        return {"message": "Sent successfully", "data": result.data[0]}
        
    except Exception as e:
        print(f"Send message error: {e}")
        raise HTTPException(500, f"Failed to send message: {str(e)}")


@router.post("/chat/assistant")
async def chat_with_assistant(data: ChatMessageRequest, current_user: Dict = Depends(get_current_user)):
    """Chat with AI recruiter assistant"""
    try:
        user_id = current_user["user_id"]
        user_role = current_user["role"]
        user_email = current_user.get("email", "User")
        
        # Build context
        context = {
            "user_role": user_role,
            "user_name": user_email.split("@")[0]
        }
        
        # Get user's recent data for context
        if user_role == "candidate":
            # Get recent applications
            apps = (
                db.get_client()
                .table("applications")
                .select("*, job:jobs(title)")
                .eq("candidate_profile_id", user_id)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            context["recent_applications"] = apps.data if apps.data else []
            
        elif user_role == "recruiter":
            # Get recent jobs
            jobs = (
                db.get_client()
                .table("jobs")
                .select("title, skills")
                .eq("recruiter_id", user_id)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            context["recent_jobs"] = jobs.data if jobs.data else []
        
        # Get AI response
        response = chat_service.get_recruiter_assistant_response(
            message=data.message,
            conversation_history=data.conversation_history,
            context=context
        )
        
        # Save conversation to database
        try:
            # Get or create conversation
            conv_result = (
                db.get_client()
                .table("chat_conversations")
                .select("*")
                .eq("user_id", user_id)
                .execute()
            )
            
            if conv_result.data:
                # Update existing
                conversation = conv_result.data[0]
                history = conversation.get("conversation_history", [])
                history.append({"role": "user", "content": data.message})
                history.append({"role": "assistant", "content": response})
                
                # Keep only last 50 messages
                history = history[-50:]
                
                db.get_client().table("chat_conversations").update({
                    "conversation_history": history,
                    "last_message_at": datetime.utcnow().isoformat()
                }).eq("id", conversation["id"]).execute()
            else:
                # Create new
                db.get_client().table("chat_conversations").insert({
                    "user_id": user_id,
                    "conversation_history": [
                        {"role": "user", "content": data.message},
                        {"role": "assistant", "content": response}
                    ],
                    "last_message_at": datetime.utcnow().isoformat()
                }).execute()
        except Exception as db_error:
            print(f"Failed to save conversation: {db_error}")
            # Continue even if save fails
        
        return {"response": response}
        
    except Exception as e:
        print(f"Chat assistant error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, "I'm having trouble connecting right now. Please try again in a moment. 🙏")


@router.delete("/chat/clear")
async def clear_chat(current_user: Dict = Depends(get_current_user)):
    """Clear chat conversation history"""
    try:
        user_id = current_user["user_id"]
        
        db.get_client().table("chat_conversations").delete().eq("user_id", user_id).execute()
        
        return {"message": "Chat cleared successfully"}
        
    except Exception as e:
        print(f"Clear chat error: {e}")
        raise HTTPException(500, f"Failed to clear chat: {str(e)}")