import json
import boto3
import logging
from datetime import datetime
from botocore.exceptions import ClientError
from PIL import Image
import pillow_heif
import os
import tempfile
import urllib.parse

# AWS Clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Constants
TABLE_NAME = "masrikdahir_image_place"
SOURCE_BUCKET = "masrikdahir-image"
DEST_BUCKET = "masrikdahir"
cloudfront = boto3.client('cloudfront')
CLOUDFRONT_DIST_ID = "E2SSEF4XZUSQ74"
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv'}

# Logger setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def convert_to_jpg_s3(key, bucket):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = os.path.join(tmpdir, os.path.basename(key))
            s3.download_file(bucket, key, local_path)

            file_name, ext = os.path.splitext(local_path)
            ext = ext.lower()

            if ext in VIDEO_EXTENSIONS:
                logger.info(f"⏩ Skipping video file in convert: {key}")
                os.remove(local_path)
                return False, None
            elif ext in ['.tiff', '.tif']:
                img = Image.open(local_path)
                new_path = f"{file_name}.jpg"
                img.convert("RGB").save(new_path, 'JPEG', quality=95)
                os.remove(local_path)
            elif ext == '.heic':
                heif_file = pillow_heif.read_heif(local_path)
                img = Image.frombytes(
                    heif_file.mode,
                    heif_file.size,
                    heif_file.data,
                    "raw",
                )
                new_path = f"{file_name}.jpg"
                img.save(new_path, 'JPEG')
                os.remove(local_path)
            elif ext == '.mp4':
                os.remove(local_path)
                return False, None
            else:
                with Image.open(local_path) as img:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')

                    if ext == '.jpg':
                        new_path = f"{file_name}_lower_case.jpg"
                    elif ext == '.jpeg':
                        new_path = f"{file_name}.jpg"
                    elif ext in ['.png', '.bmp']:
                        new_path = f"{file_name}.jpg"
                    else:
                        logger.warning(f"Unsupported format: {ext}")
                        return False, None

                    img.save(new_path, 'JPEG')
                    os.remove(local_path)

            # Upload the converted file
            s3_key = os.path.join(os.path.dirname(key), os.path.basename(new_path))
            s3.upload_file(new_path, bucket, s3_key)

            # Delete original if different
            if s3_key != key:
                s3.delete_object(Bucket=bucket, Key=key)

            return True, s3_key

    except Exception as e:
        logger.error(f"❌ Conversion failed for {key}: {e}")
        return False, None


def rename_images_in_s3_folder(folder_prefix):
    try:
        response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix=folder_prefix)
        contents = response.get("Contents", [])
        image_files = [obj["Key"] for obj in contents if obj["Key"].lower().endswith(".jpg")]

        image_files.sort()
        count = 1
        for key in image_files:
            ext = os.path.splitext(key)[1]
            new_key = os.path.join(os.path.dirname(key), f"{count}{ext}")
            if new_key != key:
                s3.copy_object(Bucket=DEST_BUCKET, CopySource={'Bucket': DEST_BUCKET, 'Key': key}, Key=new_key)
                s3.delete_object(Bucket=DEST_BUCKET, Key=key)
                logger.info(f"Renamed {key} → {new_key}")
            count += 1
        logger.info("✅ All images renamed successfully.")

    except Exception as e:
        logger.error(f"❌ Error during S3 renaming: {e}")



def write_item_to_dynamodb(table_name, item, region_name='us-east-1'):
    dynamodb = boto3.resource('dynamodb', region_name=region_name)
    table = dynamodb.Table(table_name)
    try:
        response = table.put_item(Item=item)
        logging.info(f"✅ Successfully wrote item to {table_name}: {item}")
        return response
    except ClientError as e:
        logging.error(f"❌ Failed to write item to {table_name}: {e}")
        return None

def update_image_counts_json(bucket, json_key, processed_folders):
    try:
        # Download existing JSON
        tmp_file = tempfile.NamedTemporaryFile(delete=False)
        s3.download_file(bucket, json_key, tmp_file.name)

        with open(tmp_file.name, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # For each folder, count JPGs and update the corresponding entry
        for item in data:
            folder_name = item.get("name")
            if folder_name in processed_folders:
                prefix = folder_name + "/"
                response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
                contents = response.get("Contents", [])
                jpg_count = sum(1 for obj in contents if obj["Key"].lower().endswith(".jpg") and 'Thumbnail/' not in obj["Key"])
                item["numImages"] = jpg_count

        # Save updated JSON
        with open(tmp_file.name, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        # Upload back to S3
        s3.upload_file(tmp_file.name, bucket, json_key)
        logger.info(f"✅ Updated image count in {json_key}")

    except Exception as e:
        logger.error(f"❌ Failed to update image counts in JSON: {e}")


def lambda_handler(event, context):
    table = dynamodb.Table(TABLE_NAME)
    today_str = datetime.utcnow().strftime("%Y%m%d")

    logger.info(f"Querying table for timestamp: {today_str}")

    try:
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('timestamp').eq(today_str),
            ProjectionExpression="place"
        )
        items = response.get("Items", [])
        places = [urllib.parse.unquote_plus(item["place"]) for item in items if "place" in item]
        logger.info(f"Found {len(places)} places for today.")
    except ClientError as e:
        logger.error(f"❌ Error querying DynamoDB: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps("Failed to query DynamoDB")
        }

    success_count = 0
    for key in places:
        ext = os.path.splitext(key)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            logger.info(f"⏩ Skipping video file: {key}")
            continue  # Skip video files entirely
        try:
            # Copy from SOURCE_BUCKET to DEST_BUCKET
            s3.copy(
                CopySource={'Bucket': SOURCE_BUCKET, 'Key': key},
                Bucket=DEST_BUCKET,
                Key=key
            )
            logger.info(f"Copied: {key}")

            # Convert to .jpg if necessary
            converted, new_key = convert_to_jpg_s3(key, DEST_BUCKET)
            if converted:
                success_count += 1

        except ClientError as e:
            logger.error(f"❌ Failed processing {key}: {e}")

    # Extract all unique top-level prefixes
    folders_to_rename = set(os.path.dirname(p) for p in places)

    for folder_prefix in folders_to_rename:
        # Normalize to avoid trailing slashes
        parts = folder_prefix.strip('/').split('/')
        if parts and parts[-1].lower() == 'thumbnail':
            logger.info(f"⏩ Skipping rename in: {folder_prefix} (Thumbnail folder)")
            continue
        rename_images_in_s3_folder(folder_prefix)

    try:
        invalidation = cloudfront.create_invalidation(
            DistributionId=CLOUDFRONT_DIST_ID,
            InvalidationBatch={
                'Paths': {
                    'Quantity': 1,
                    'Items': ['/*']
                },
                'CallerReference': str(datetime.utcnow().timestamp())
            }
        )
        logger.info(f"✅ CloudFront invalidation created: {invalidation['Invalidation']['Id']}")
    except ClientError as e:
        logger.error(f"❌ Failed to create CloudFront invalidation: {e}")

    # Update image counts in the master JSON
    update_image_counts_json(
        bucket=DEST_BUCKET,
        json_key="Json/image.json",
        processed_folders={os.path.dirname(p).split('/')[0] for p in places}
    )

    # Write completion marker to DynamoDB
    write_item_to_dynamodb(
        table_name="last_updated",
        item={
            "key": "ImageProcessor",
            "Result": "Success",
            "Timestamp": datetime.utcnow().isoformat() + "Z"
        }
    )

    return {
        "statusCode": 200,
        "body": json.dumps(f"Copied and processed {success_count} of {len(places)} images.")
    }
