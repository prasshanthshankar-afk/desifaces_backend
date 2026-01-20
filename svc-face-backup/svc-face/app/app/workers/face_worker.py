# services/svc-face/app/app/workers/face_worker.py
from __future__ import annotations
import asyncio
import logging
import signal
import sys
from app.db import get_pool, close_pool
from app.services.face_orchestrator import FaceOrchestrator

logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG
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
    
    try:
        logger.info("Connecting to database...")
        pool = await get_pool()
        logger.info("Database connected!")
        
        logger.info("Initializing orchestrator...")
        orch = FaceOrchestrator(pool)
        logger.info("Orchestrator ready!")
        
        logger.info("Entering main loop...")
        
        while not shutdown_event.is_set():
            try:
                # Claim next job
                from app.repos.face_jobs_repo import FaceJobsRepo
                jobs_repo = FaceJobsRepo(pool)
                
                logger.debug("Claiming jobs...")
                job_ids = await jobs_repo.claim_next_jobs(studio_type="face", limit=1)
                
                if not job_ids:
                    logger.debug("No jobs available, sleeping...")
                    await asyncio.sleep(5)
                    continue
                
                job_id = job_ids[0]
                logger.info(f"Processing job: {job_id}")
                
                # Process job
                await orch.run_job(job_id)
                logger.info(f"Job {job_id} completed")
                
            except Exception as e:
                logger.exception("Worker loop error", extra={"error": str(e)})
                await asyncio.sleep(5)
    
    except Exception as e:
        logger.exception("Fatal worker error", extra={"error": str(e)})
    
    finally:
        logger.info("Face worker shutting down...")
        await close_pool()

def main():
    """Entry point"""
    logger.info("Main function called")
    
    # Register shutdown handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    logger.info("Starting event loop...")
    
    # Run worker
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
    except Exception as e:
        logger.exception("Main error", extra={"error": str(e)})
    finally:
        logger.info("Worker stopped")

if __name__ == "__main__":
    logger.info("Script starting...")
    main()
