# services/svc-face/app/app/services/azure_storage_service.py
from __future__ import annotations
from typing import Tuple
from datetime import datetime, timedelta
import httpx
import base64
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions, ContentSettings
from app.config import settings

class AzureStorageService:
    """Azure Blob Storage operations for face images"""
    
    def __init__(self):
        self.connection_string = settings.AZURE_STORAGE_CONNECTION_STRING
        self.container = settings.FACE_OUTPUT_CONTAINER
        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)
    
    async def download_image(self, url: str) -> bytes:
        """Download image from URL"""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            if response.status_code != 200:
                raise Exception(f"Failed to download image: {response.status_code}")
            return response.content
    
    async def upload_image(
        self,
        image_bytes: bytes,
        user_id: str,
        job_id: str,
        variant: int,
        content_type: str = "image/jpeg"
    ) -> Tuple[str, str]:
        """
        Upload image to Azure Blob Storage.
        
        Returns:
            (storage_path, blob_url_with_sas)
        """
        # Generate storage path: user_id/job_id/variant_N.jpg
        blob_name = f"{user_id}/{job_id}/variant_{variant}.jpg"
        
        # Get blob client
        blob_client = self.blob_service.get_blob_client(
            container=self.container,
            blob=blob_name
        )
        
        # Upload with proper ContentSettings object
        blob_client.upload_blob(
            image_bytes,
            overwrite=True,
            content_settings=ContentSettings(
                content_type=content_type
            )
        )
        
        # Generate SAS URL (24 hour expiry)
        sas_url = self._generate_sas_url(blob_name)
        
        return blob_name, sas_url
    
    async def upload_from_url(
        self,
        url: str,
        user_id: str,
        job_id: str,
        variant: int
    ) -> Tuple[str, str]:
        """
        Upload image from URL or data URL to Azure Blob Storage.
        Supports both HTTP URLs and data: URLs from fal.ai.
        
        Returns:
            (storage_path, blob_url_with_sas)
        """
        try:
            if url.startswith('data:'):
                # Handle data URL (base64 image from fal.ai)
                # Format: data:image/jpeg;base64,/9j/4AAQSkZJRg...
                header, data = url.split(',', 1)
                image_bytes = base64.b64decode(data)
                
                # Extract content type from data URL
                content_type = "image/jpeg"  # default
                if 'image/' in header:
                    type_part = header.split('image/')[1].split(';')[0]
                    content_type = f"image/{type_part}"
                    
            else:
                # Handle HTTP URL
                image_bytes = await self.download_image(url)
                content_type = "image/jpeg"
            
            # Upload using the existing upload_image method
            return await self.upload_image(image_bytes, user_id, job_id, variant, content_type)
            
        except Exception as e:
            raise Exception(f"Failed to upload from URL: {str(e)}")
    
    def _generate_sas_url(self, blob_name: str, hours: int = 24) -> str:
        """Generate SAS URL for blob access"""
        # Parse connection string for account info
        conn_parts = dict(item.split('=', 1) for item in self.connection_string.split(';') if '=' in item)
        account_name = conn_parts.get('AccountName')
        account_key = conn_parts.get('AccountKey')
        
        if not account_name or not account_key:
            raise Exception("Could not parse storage account credentials")
        
        # Generate SAS token
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=hours)
        )
        
        # Build full URL
        blob_url = f"https://{account_name}.blob.core.windows.net/{self.container}/{blob_name}?{sas_token}"
        
        return blob_url
    
    async def regenerate_sas_url(self, storage_path: str, hours: int = 24) -> str:
        """Regenerate SAS URL for existing blob"""
        return self._generate_sas_url(storage_path, hours)