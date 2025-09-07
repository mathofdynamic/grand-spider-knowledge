#!/usr/bin/env python3
"""
Comprehensive Website Knowledge Base Generation Test Suite
Tests all 10 websites with full PDF generation and CSV output.
All PDFs are saved to the reports/ folder.
"""

import os
import sys
import time
import json
import csv
import requests
import logging
from datetime import datetime
from typing import List, Dict, Any
import subprocess
import threading
from pathlib import Path
import arabic_reshaper
from bidi.algorithm import get_display
import markdown
import traceback

# Add parent directory to path to import grand_spider
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import required libraries for PDF generation
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("Warning: reportlab not available. PDF generation will be skipped.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('test_run.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Test websites - All 10 websites to test
TEST_WEBSITES = [
    "https://netoffshop.ir/",
    "https://www.malek-doorbin.com/",
    "https://alandview.ir/",
    "https://miladcam.com/",
    "https://www.dourbinet.com/",
    "https://www.manzel.ir/",
    "https://shikwall.ir/",
    "https://kaqazdivari-avindecor.ir/",
    "https://tehranposter.ir/"
]

# API Configuration - Hardcoded API key
API_BASE_URL = "http://localhost:5000"
API_KEY = "this_is_very_stupid_key_for_this_api"

class WebsiteTester:
    """Main class for testing website knowledge base generation."""
    
    def __init__(self):
        self.results = []
        self.start_time = None
        # Default preferred Persian font path
        self.font_path = "IRANSANS_MEDIUM_0.TTF"
        
        # Ensure a usable Persian font is available for PDF generation
        if not os.path.exists(self.font_path):
            # Try alternative font names/locations commonly used
            alternative_fonts = [
                "IRANSans.ttf",
                "IRANSans-Medium.ttf",
                "B Nazanin.ttf",
                "fonts/IRANSANS_MEDIUM_0.TTF"
            ]
            for alt_font in alternative_fonts:
                if os.path.exists(alt_font):
                    self.font_path = alt_font
                    logger.info(f"Using alternative font: {alt_font}")
                    break
            else:
                logger.warning(
                    "No Persian font file found. PDF will use default Latin font which may not render Persian correctly."
                )
                self.font_path = None
    
    def start_grand_spider_server(self):
        """Start the grand_spider.py server in background."""
        logger.info("Starting Grand Spider server...")
        try:
            # Start the server in background
            self.server_process = subprocess.Popen(
                [sys.executable, "../grand_spider.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            
            # Wait for server to start
            time.sleep(5)
            
            # Test if server is running
            try:
                response = requests.get(f"{API_BASE_URL}/api/health", timeout=10)
                if response.status_code == 200:
                    logger.info("Grand Spider server started successfully")
                    return True
                else:
                    logger.error(f"Server health check failed: {response.status_code}")
                    return False
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to connect to server: {e}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
            return False
    
    def stop_grand_spider_server(self):
        """Stop the grand_spider.py server."""
        if hasattr(self, 'server_process'):
            logger.info("Stopping Grand Spider server...")
            self.server_process.terminate()
            self.server_process.wait()
            logger.info("Server stopped")
    
    def test_website(self, url: str) -> Dict[str, Any]:
        """Test a single website and return results."""
        logger.info(f"Testing website: {url}")
        
        result = {
            "url": url,
            "status": "pending",
            "job_id": None,
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "duration_seconds": None,
            "error": None,
            "knowledge_base": None,
            "metadata": None,
            "website_colors": None,
            "pages_processed": 0,
            "cost_estimation": None
        }
        
        try:
            # Start knowledge base generation job
            response = requests.post(
                f"{API_BASE_URL}/api/generate-knowledge-base",
                headers={"api-key": API_KEY},
                json={
                    "url": url,
                    "max_pages": 20,
                    "use_selenium": True
                },
                timeout=30
            )
            
            if response.status_code != 202:
                result["status"] = "failed"
                result["error"] = f"Failed to start job: {response.status_code} - {response.text}"
                return result
            
            job_data = response.json()
            job_id = job_data["job_id"]
            result["job_id"] = job_id
            
            logger.info(f"Job started for {url} with ID: {job_id}")
            
            # Poll for job completion
            max_wait_time = 1800  # 30 minutes
            poll_interval = 10    # 10 seconds
            elapsed_time = 0
            
            while elapsed_time < max_wait_time:
                time.sleep(poll_interval)
                elapsed_time += poll_interval
                
                # Check job status
                status_response = requests.get(
                    f"{API_BASE_URL}/api/jobs/{job_id}",
                    headers={"api-key": API_KEY},
                    timeout=30
                )
                
                if status_response.status_code != 200:
                    result["status"] = "failed"
                    result["error"] = f"Failed to get job status: {status_response.status_code}"
                    return result
                
                job_status = status_response.json()
                current_status = job_status.get("status")
                progress = job_status.get("progress", "Unknown")
                progress_fa = job_status.get("progress_fa", "Unknown")
                
                logger.info(f"Job {job_id} status: {current_status} - {progress}")
                logger.info(f"Progress (FA): {progress_fa}")
                
                # Debug: Check if website colors are being extracted
                if "website_colors" in job_status:
                    colors = job_status.get("website_colors", {})
                    logger.info(f"Website colors found: {colors}")
                
                if current_status == "completed":
                    # Job completed successfully
                    result["status"] = "completed"
                    result["end_time"] = datetime.now().isoformat()
                    result["duration_seconds"] = elapsed_time
                    result["knowledge_base"] = job_status.get("final_knowledge_base", "")
                    result["metadata"] = job_status.get("comprehensive_analysis", {})
                    result["website_colors"] = job_status.get("website_colors", {})
                    result["pages_processed"] = job_status.get("extracted_pages_count", 0)
                    result["cost_estimation"] = job_status.get("cost_estimation", {})
                    
                    logger.info(f"Job {job_id} completed successfully for {url}")
                    break
                    
                elif current_status == "failed":
                    # Job failed
                    result["status"] = "failed"
                    result["end_time"] = datetime.now().isoformat()
                    result["duration_seconds"] = elapsed_time
                    result["error"] = job_status.get("error", "Unknown error")
                    
                    logger.error(f"Job {job_id} failed for {url}: {result['error']}")
                    break
                    
                elif current_status == "running":
                    # Job still running, continue waiting
                    continue
                else:
                    # Unknown status
                    logger.warning(f"Unknown job status: {current_status}")
                    continue
            
            if elapsed_time >= max_wait_time:
                result["status"] = "timeout"
                result["end_time"] = datetime.now().isoformat()
                result["duration_seconds"] = elapsed_time
                result["error"] = "Job timed out after 30 minutes"
                logger.error(f"Job {job_id} timed out for {url}")
            
        except requests.exceptions.RequestException as e:
            result["status"] = "failed"
            result["error"] = f"Request error: {str(e)}"
            logger.error(f"Request error for {url}: {e}")
        except Exception as e:
            result["status"] = "failed"
            result["error"] = f"Unexpected error: {str(e)}"
            logger.error(f"Unexpected error for {url}: {e}")
        
        return result
    
    def save_results_to_csv(self, results: List[Dict[str, Any]]):
        """Save test results to CSV file."""
        csv_path = "website_test_results.csv"
        
        # Define CSV headers
        headers = [
            "url", "status", "job_id", "start_time", "end_time", "duration_seconds",
            "pages_processed", "main_background_color", "primary_brand_color",
            "background_color_description", "brand_color_description",
            "total_cost_usd", "prompt_tokens", "completion_tokens",
            "error", "knowledge_base_length"
        ]
        
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=headers)
                writer.writeheader()
                
                for result in results:
                    # Extract website colors
                    website_colors = result.get("website_colors", {})
                    
                    row = {
                        "url": result.get("url", ""),
                        "status": result.get("status", ""),
                        "job_id": result.get("job_id", ""),
                        "start_time": result.get("start_time", ""),
                        "end_time": result.get("end_time", ""),
                        "duration_seconds": result.get("duration_seconds", ""),
                        "pages_processed": result.get("pages_processed", 0),
                        "main_background_color": website_colors.get("main_background_color", ""),
                        "primary_brand_color": website_colors.get("primary_brand_color", ""),
                        "background_color_description": website_colors.get("background_color_description", ""),
                        "brand_color_description": website_colors.get("brand_color_description", ""),
                        "total_cost_usd": result.get("cost_estimation", {}).get("total_cost_usd", ""),
                        "prompt_tokens": result.get("cost_estimation", {}).get("prompt_tokens", ""),
                        "completion_tokens": result.get("cost_estimation", {}).get("completion_tokens", ""),
                        "error": result.get("error", ""),
                        "knowledge_base_length": len(result.get("knowledge_base", ""))
                    }
                    writer.writerow(row)
            
            logger.info(f"Results saved to CSV: {csv_path}")
            
        except Exception as e:
            logger.error(f"Failed to save CSV results: {e}")

    def save_knowledge_base_to_pdf(self, result: Dict[str, Any]):
        """Save knowledge base to a beautiful Persian PDF in reports folder.
        - Registers a Persian font if available
        - Applies RTL shaping and bidi for Persian text
        - Uses consistent spacing and color styles
        - Saves to reports/ folder
        """
        if not PDF_AVAILABLE:
            logger.warning("PDF generation not available - skipping PDF creation")
            return
        
        if not result.get("knowledge_base"):
            logger.warning(f"No knowledge base content for {result['url']} - skipping PDF")
            return
        
        try:
            from reportlab.lib import colors
            
            # Create filename from URL and save to reports folder
            url_parts = result["url"].replace("https://", "").replace("http://", "").replace("/", "_")
            if url_parts.endswith("_"):
                url_parts = url_parts[:-1]
            pdf_path = f"reports/knowledge_base_{url_parts}.pdf"
            
            # Create PDF document with proper margins
            doc = SimpleDocTemplate(
                pdf_path,
                pagesize=A4,
                rightMargin=60,
                leftMargin=60,
                topMargin=60,
                bottomMargin=60
            )
            styles = getSampleStyleSheet()
            
            # Register Persian font if available
            font_name = 'Helvetica'  # Default font
            bold_font_name = 'Helvetica-Bold'
            if self.font_path and os.path.exists(self.font_path):
                try:
                    pdfmetrics.registerFont(TTFont('Persian', self.font_path))
                    # Try to register bold variant if available
                    try:
                        bold_font_path = self.font_path.replace('.ttf', '_Bold.ttf').replace('.TTF', '_Bold.TTF')
                        if os.path.exists(bold_font_path):
                            pdfmetrics.registerFont(TTFont('Persian-Bold', bold_font_path))
                            bold_font_name = 'Persian-Bold'
                        else:
                            bold_font_name = 'Persian'
                    except Exception:
                        bold_font_name = 'Persian'
                    font_name = 'Persian'
                    logger.info("Persian font registered successfully")
                except Exception as e:
                    logger.warning(f"Failed to register Persian font: {e}")
                    font_name = 'Helvetica'
                    bold_font_name = 'Helvetica-Bold'
            else:
                logger.warning("Persian font file not found, using default font")
            
            # Define colors
            primary_blue = colors.Color(0.2, 0.4, 0.8, 1)
            secondary_blue = colors.Color(0.3, 0.5, 0.7, 1)
            accent_color = colors.Color(0.1, 0.6, 0.4, 1)
            text_color = colors.Color(0.2, 0.2, 0.2, 1)
            
            # Styles
            title_style = ParagraphStyle(
                'TitleStyle', fontName=bold_font_name, fontSize=20, textColor=primary_blue,
                alignment=2, spaceAfter=24, spaceBefore=12, leading=26
            )
            main_heading_style = ParagraphStyle(
                'MainHeadingStyle', fontName=bold_font_name, fontSize=16, textColor=secondary_blue,
                alignment=2, spaceAfter=18, spaceBefore=20, leading=20
            )
            sub_heading_style = ParagraphStyle(
                'SubHeadingStyle', fontName=bold_font_name, fontSize=14, textColor=secondary_blue,
                alignment=2, spaceAfter=14, spaceBefore=16, leading=18
            )
            metadata_style = ParagraphStyle(
                'MetadataStyle', fontName=font_name, fontSize=11, textColor=accent_color,
                alignment=2, spaceAfter=8, spaceBefore=4, leading=14
            )
            body_style = ParagraphStyle(
                'BodyStyle', fontName=font_name, fontSize=12, textColor=text_color,
                alignment=2, spaceAfter=10, spaceBefore=4, leading=16, leftIndent=0, rightIndent=0
            )
            bold_body_style = ParagraphStyle(
                'BoldBodyStyle', parent=body_style, fontName=bold_font_name, spaceAfter=12, spaceBefore=8
            )
            list_style = ParagraphStyle(
                'ListStyle', parent=body_style, leftIndent=20, bulletIndent=10, spaceAfter=6, spaceBefore=3
            )
            
            # Helper to escape text for ReportLab paragraphs
            def _escape_for_reportlab(text: str) -> str:
                """Escape special XML characters to prevent paraparser errors."""
                try:
                    from xml.sax.saxutils import escape as xml_escape
                    return xml_escape(text, {"'": "&#39;"})
                except Exception:
                    # Fallback simple replacements
                    return (
                        text.replace('&', '&amp;')
                            .replace('<', '&lt;')
                            .replace('>', '&gt;')
                    )

            # Build PDF content
            story = []
            
            # Title
            title_text = f"گزارش پایگاه دانش وب‌سایت"
            reshaped_title = arabic_reshaper.reshape(title_text)
            bidi_title = get_display(reshaped_title)
            story.append(Paragraph(_escape_for_reportlab(bidi_title), title_style))
            
            # URL subtitle
            url_text = result['url']
            story.append(Paragraph(_escape_for_reportlab(url_text), metadata_style))
            story.append(Spacer(1, 20))
            
            # Metadata
            metadata_title = "اطلاعات کلی"
            reshaped_meta_title = arabic_reshaper.reshape(metadata_title)
            bidi_meta_title = get_display(reshaped_meta_title)
            story.append(Paragraph(_escape_for_reportlab(bidi_meta_title), main_heading_style))
            
            website_colors = result.get('website_colors') or {}
            cost_estimation = result.get('cost_estimation') or {}
            metadata_items = [
                f"وضعیت پردازش: {result['status']}",
                f"تعداد صفحات پردازش شده: {result.get('pages_processed', 0)}",
                f"مدت زمان پردازش: {result.get('duration_seconds', 0)} ثانیه",
                f"رنگ اصلی پس‌زمینه: {website_colors.get('main_background_color', 'مشخص نشده')}",
                f"رنگ اصلی برند: {website_colors.get('primary_brand_color', 'مشخص نشده')}",
                f"هزینه تقریبی: ${cost_estimation.get('total_cost_usd', '0.00')}",
                f"تاریخ و زمان تولید: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ]
            for item in metadata_items:
                reshaped_item = arabic_reshaper.reshape(item)
                bidi_item = get_display(reshaped_item)
                story.append(Paragraph(_escape_for_reportlab(bidi_item), metadata_style))
            story.append(Spacer(1, 24))
            
            # Knowledge base content
            kb_text = result["knowledge_base"]
            if not kb_text:
                return
            lines = kb_text.split('\n')
            current_paragraph = []
            in_list = False
            for line in lines:
                line = line.strip()
                if not line:
                    if current_paragraph:
                        para_text = ' '.join(current_paragraph)
                        if para_text:
                            reshaped_para = arabic_reshaper.reshape(para_text)
                            bidi_para = get_display(reshaped_para)
                            story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
                        current_paragraph = []
                    if in_list:
                        story.append(Spacer(1, 12))
                        in_list = False
                    else:
                        story.append(Spacer(1, 8))
                    continue
                if line.startswith('# ') and not line.startswith('## '):
                    if current_paragraph:
                        para_text = ' '.join(current_paragraph)
                        reshaped_para = arabic_reshaper.reshape(para_text)
                        bidi_para = get_display(reshaped_para)
                        story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
                        current_paragraph = []
                    heading_text = line[2:].strip()
                    reshaped_heading = arabic_reshaper.reshape(heading_text)
                    bidi_heading = get_display(reshaped_heading)
                    story.append(Paragraph(_escape_for_reportlab(bidi_heading), main_heading_style))
                    in_list = False
                    continue
                elif line.startswith('## '):
                    if current_paragraph:
                        para_text = ' '.join(current_paragraph)
                        reshaped_para = arabic_reshaper.reshape(para_text)
                        bidi_para = get_display(reshaped_para)
                        story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
                        current_paragraph = []
                    heading_text = line[3:].strip()
                    reshaped_heading = arabic_reshaper.reshape(heading_text)
                    bidi_heading = get_display(reshaped_heading)
                    story.append(Paragraph(_escape_for_reportlab(bidi_heading), sub_heading_style))
                    in_list = False
                    continue
                elif '**' in line:
                    if current_paragraph:
                        para_text = ' '.join(current_paragraph)
                        reshaped_para = arabic_reshaper.reshape(para_text)
                        bidi_para = get_display(reshaped_para)
                        story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
                        current_paragraph = []
                    bold_text = line.replace('**', '').strip()
                    reshaped_bold = arabic_reshaper.reshape(bold_text)
                    bidi_bold = get_display(reshaped_bold)
                    story.append(Paragraph(_escape_for_reportlab(bidi_bold), bold_body_style))
                    in_list = False
                    continue
                elif line.startswith('- ') or line.startswith('* '):
                    if current_paragraph:
                        para_text = ' '.join(current_paragraph)
                        reshaped_para = arabic_reshaper.reshape(para_text)
                        bidi_para = get_display(reshaped_para)
                        story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
                        current_paragraph = []
                    list_text = '• ' + line[2:].strip()
                    reshaped_list = arabic_reshaper.reshape(list_text)
                    bidi_list = get_display(reshaped_list)
                    story.append(Paragraph(_escape_for_reportlab(bidi_list), list_style))
                    in_list = True
                    continue
                else:
                    clean_line = line.replace('***', '').replace('---', '').replace('___', '').strip()
                    if clean_line:
                        current_paragraph.append(clean_line)
            if current_paragraph:
                para_text = ' '.join(current_paragraph)
                reshaped_para = arabic_reshaper.reshape(para_text)
                bidi_para = get_display(reshaped_para)
                story.append(Paragraph(_escape_for_reportlab(bidi_para), body_style))
            
            # Footer
            story.append(Spacer(1, 30))
            footer_line = Paragraph(
                "─" * 50,
                ParagraphStyle('Separator', fontSize=8, textColor=colors.lightgrey, alignment=1, spaceAfter=12)
            )
            story.append(footer_line)
            generated_text = f"تولید شده در تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            reshaped_footer = arabic_reshaper.reshape(generated_text)
            bidi_footer = get_display(reshaped_footer)
            footer_style = ParagraphStyle('Footer', fontSize=10, textColor=colors.grey, alignment=2)
            story.append(Paragraph(_escape_for_reportlab(bidi_footer), footer_style))
            
            # Build PDF
            doc.build(story)
            logger.info(f"Beautiful Persian PDF saved: {pdf_path}")
        except Exception as e:
            logger.error(f"Failed to create PDF for {result['url']}: {e}")
            logger.error(f"Error details: {traceback.format_exc()}")

    def run_all_tests(self):
        """Run tests for all websites."""
        logger.info("Starting website knowledge base generation tests...")
        self.start_time = datetime.now()
        
        # Start the server
        if not self.start_grand_spider_server():
            logger.error("Failed to start Grand Spider server. Exiting.")
            return
        
        try:
            # Test each website
            for i, url in enumerate(TEST_WEBSITES, 1):
                logger.info(f"Testing website {i}/{len(TEST_WEBSITES)}: {url}")
                
                result = self.test_website(url)
                self.results.append(result)
                
                # Save individual PDF to reports folder
                if result["status"] == "completed":
                    self.save_knowledge_base_to_pdf(result)
                    logger.info(f"✅ Website {i}/{len(TEST_WEBSITES)} completed: {url}")
                else:
                    logger.error(f"❌ Website {i}/{len(TEST_WEBSITES)} failed: {url} - {result.get('error', 'Unknown error')}")
                
                # Small delay between tests
                time.sleep(5)
            
            # Save all results to CSV
            self.save_results_to_csv(self.results)
            
            # Print summary
            self.print_summary()
            
        finally:
            # Stop the server
            self.stop_grand_spider_server()
    
    def print_summary(self):
        """Print test summary."""
        total_tests = len(self.results)
        completed = len([r for r in self.results if r["status"] == "completed"])
        failed = len([r for r in self.results if r["status"] == "failed"])
        timeout = len([r for r in self.results if r["status"] == "timeout"])
        
        total_duration = (datetime.now() - self.start_time).total_seconds()
        
        logger.info("=" * 60)
        logger.info("TEST SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total websites tested: {total_tests}")
        logger.info(f"Completed successfully: {completed}")
        logger.info(f"Failed: {failed}")
        logger.info(f"Timed out: {timeout}")
        logger.info(f"Total test duration: {total_duration:.2f} seconds")
        logger.info("=" * 60)
        
        # Print individual results
        for result in self.results:
            status_emoji = "✅" if result["status"] == "completed" else "❌"
            logger.info(f"{status_emoji} {result['url']} - {result['status']}")
            if result["status"] == "completed":
                logger.info(f"   Pages: {result.get('pages_processed', 0)}, "
                          f"Duration: {result.get('duration_seconds', 0)}s, "
                          f"Cost: ${result.get('cost_estimation', {}).get('total_cost_usd', '0.00')}")
            elif result["error"]:
                logger.info(f"   Error: {result['error']}")

def main():
    """Main function to run the test suite."""
    logger.info("🚀 Starting comprehensive website knowledge base generation tests...")
    logger.info(f"📋 Testing {len(TEST_WEBSITES)} websites:")
    for i, url in enumerate(TEST_WEBSITES, 1):
        logger.info(f"   {i}. {url}")
    
    tester = WebsiteTester()
    tester.run_all_tests()
    
    logger.info("🎉 All tests completed! Check the reports/ folder for PDF files and website_test_results.csv for summary data.")

if __name__ == "__main__":
    main()
