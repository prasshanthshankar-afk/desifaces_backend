# services/svc-face/app/app/workers/face_worker.py
from __future__ import annotations
import asyncio
import logging
import signal
import sys
from app.db import get_pool, close_pool
from app.services.creator_orchestrator import CreatorOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("face_worker")

shutdown_event = asyncio.Event()

def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    logger.info("Shutdown signal received")
    shutdown_event.set()

async def worker_loop():
    """Main worker loop"""
    logger.info("Face worker starting...")
    
    pool = await get_pool()
    orch = CreatorOrchestrator(pool)
    
    while not shutdown_event.is_set():
        try:
            # Claim next job
            from app.repos.face_jobs_repo import FaceJobsRepo
            jobs_repo = FaceJobsRepo(pool)
            
            job_ids = await jobs_repo.claim_next_jobs(studio_type="face", limit=1)
            
            if not job_ids:
                # No jobs available, wait
                await asyncio.sleep(5)
                continue
            
            job_id = job_ids[0]
            logger.info(f"Processing job: {job_id}")
            
            # Process job
            await orch.run_job(job_id)
            
        except Exception as e:
            logger.exception("Worker error", extra={"error": str(e)})
            await asyncio.sleep(5)
    
    logger.info("Face worker shutting down...")
    await close_pool()

def main():
    """Entry point"""
    # Register shutdown handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    # Run worker
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
    finally:
        logger.info("Worker stopped")

if __name__ == "__main__":
    main()