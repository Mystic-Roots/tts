import streamlit as st
import asyncio
import re
import os
import wave
import struct
import datetime
from pathlib import Path
import tempfile
import time
import logging
import hashlib
import threading
import requests
import json
import base64
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
from dotenv import load_dotenv

# Load environment variables for ElevenLabs
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import edge-tts directly from the repository package
try:
    # First try to import from installed package
    import edge_tts
    from edge_tts import VoicesManager
    from edge_tts.exceptions import EdgeTTSException
except ImportError:
    # If not available, notify the user
    st.error("Please install edge-tts: `pip install edge-tts`")
    st.stop()

# Try to import elevenlabs
try:
    from elevenlabs.client import ElevenLabs
except ImportError:
    # If not available, notify the user but don't stop
    HAS_ELEVENLABS = False
    logger.warning("ElevenLabs SDK not installed. Installing it is recommended for best experience.")
else:
    HAS_ELEVENLABS = True

# Cache directory for TTS audio segments
CACHE_DIR = Path("tts_cache")
CACHE_DIR.mkdir(exist_ok=True)

# Connection settings optimized for slow connections
CONNECTION_SETTINGS = {
    "timeout": 60,       # Connection timeout in seconds
    "max_retries": 5,    # Number of retry attempts
    "retry_delay": 3,    # Delay between retries in seconds
    "use_fallback": False,  # No fallback needed with improved edge-tts
    "batch_size": 5,      # Number of subtitles to process in parallel
    "use_cache": True,    # Cache generated audio to avoid redundant requests
    "chunk_timeout": 10,  # Timeout for individual audio chunks
    "exponential_backoff": True  # Use exponential backoff for retries
}

#################################################
# TTS Engine Implementation Classes
#################################################

class EdgeTTSEngine:
    """Edge TTS Engine with robust implementation for both testing and production"""
    
    def __init__(self):
        self.session = None
    
    async def _create_communicate_instance(self, text, voice_id, rate="+0%", volume="+0%"):
        """Create a consistent Communicate instance with proper settings"""
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice_id,
            rate=rate,
            volume=volume
        )
        
        # Set custom timeout for better reliability
        if self.session:
            communicate._session = self.session
        return communicate
    
    async def _ensure_session(self, timeout=60):
        """Ensure we have a properly configured aiohttp session"""
        import aiohttp
        
        if self.session:
            # Close existing session if it exists
            try:
                await self.session.close()
            except:
                pass
                
        # Create custom ClientSession with timeout
        client_timeout = aiohttp.ClientTimeout(
            total=timeout, 
            sock_connect=30,
            sock_read=CONNECTION_SETTINGS["chunk_timeout"]
        )
        
        # Use custom timeout session with TCPConnector configuration for better reliability
        connector = aiohttp.TCPConnector(
            force_close=True,
            enable_cleanup_closed=True,
            limit=4,
            ttl_dns_cache=300,
            use_dns_cache=True
        )
        
        # Set headers with longer keep-alive
        headers = {
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=60, max=100"
        }
        
        # Create a new session
        self.session = aiohttp.ClientSession(
            timeout=client_timeout, 
            connector=connector,
            headers=headers
        )
    
    def get_voices(self):
        """Get Croatian and British voices from Edge TTS"""
        try:
            voices_manager = VoicesManager()
            voices = asyncio.run(voices_manager.fetch_voices_list())
            croatian_voices = {}
            british_voices = {}
            
            for voice in voices:
                locale = voice.get("Locale", "")
                if locale.startswith("hr-HR"):
                    name = voice.get("ShortName", "").replace("hr-HR-", "").replace("Neural", "")
                    croatian_voices[f"{name} 🇭🇷"] = voice.get("ShortName")
                elif locale.startswith("en-GB"):
                    name = voice.get("ShortName", "").replace("en-GB-", "").replace("Neural", "")
                    british_voices[f"{name} 🇬🇧"] = voice.get("ShortName")
            
            # Fallback if no Croatian voices found
            if not croatian_voices:
                croatian_voices = {
                    "Srećko 🇭🇷": "hr-HR-SreckoNeural",
                    "Gabriela 🇭🇷": "hr-HR-GabrielaNeural"
                }
            
            # Combine voices - Croatian first, then British
            combined_voices = {}
            
            # Add section headers
            combined_voices["--- CROATIAN VOICES ---"] = "heading_separator"
            combined_voices.update(croatian_voices)
            combined_voices["--- BRITISH VOICES ---"] = "heading_separator"
            combined_voices.update(british_voices)
            
            return combined_voices
        except Exception as e:
            logger.error(f"Error fetching Edge TTS voices: {e}")
            return {"Srećko 🇭🇷": "hr-HR-SreckoNeural", "Gabriela 🇭🇷": "hr-HR-GabrielaNeural"}
    
    def test_connectivity(self, voice_id="hr-HR-SreckoNeural"):
        """Test connectivity to Edge TTS service"""
        try:
            return asyncio.run(self._test_connectivity_async(voice_id))
        except Exception as e:
            logger.error(f"Error testing Edge TTS connectivity: {e}")
            return False
    
    async def _test_connectivity_async(self, voice_id="hr-HR-SreckoNeural"):
        """Async implementation of connectivity test"""
        try:
            await self._ensure_session()
            communicate = await self._create_communicate_instance("Test connection", voice_id)
            
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    return True
            return False
        except Exception as e:
            logger.error(f"Async error testing Edge TTS: {e}")
            return False
        finally:
            if self.session:
                await self.session.close()
                self.session = None
    
    async def _generate_audio_async(self, text, voice_id, rate="+0%", volume="+0%"):
        """Generate audio data asynchronously using Edge TTS with robust error handling"""
        last_error = None
        max_retries = CONNECTION_SETTINGS["max_retries"]
        retry_delay = CONNECTION_SETTINGS["retry_delay"]
        exponential_backoff = CONNECTION_SETTINGS["exponential_backoff"]
        
        for attempt in range(max_retries + 1):
            try:
                # Create new session on each attempt for better reliability
                await self._ensure_session(CONNECTION_SETTINGS["timeout"])
                
                # Create communicate instance
                communicate = await self._create_communicate_instance(text, voice_id, rate, volume)
                
                # Collect audio data with timeout protection
                audio_data = bytearray()
                
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_data.extend(chunk["data"])
                
                return bytes(audio_data), None
                
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Edge TTS error on attempt {attempt+1}/{max_retries+1}: {str(e)}")
                
                # Close session before retry
                if self.session:
                    try:
                        await self.session.close()
                    except:
                        pass
                    self.session = None
                    
                if attempt < max_retries:
                    # Calculate delay with exponential backoff
                    if exponential_backoff:
                        current_delay = retry_delay * (2 ** attempt)
                        current_delay = min(current_delay, 30)  # Cap at 30 seconds
                    else:
                        current_delay = retry_delay
                        
                    await asyncio.sleep(current_delay)
        
        return None, f"All Edge TTS attempts failed: {last_error}"
    
    def generate_audio_for_testing(self, text, voice_id, rate="+0%", volume="+0%"):
        """Generate audio for testing purposes using the same robust method as production"""
        try:
            audio_bytes, error = asyncio.run(self._generate_audio_async(text, voice_id, rate, volume))
            
            if error:
                logger.error(f"Error in test audio generation: {error}")
                return None
                
            # Save to temp file for streamlit audio playback
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
                temp_file.write(audio_bytes)
                return temp_file.name
                
        except Exception as e:
            logger.error(f"Error generating Edge TTS test audio: {e}")
            return None
    
    def generate_audio(self, text, voice_id, rate="+0%", volume="+0%"):
        """Generate audio bytes directly - to be used for both testing and production"""
        try:
            audio_bytes, error = asyncio.run(self._generate_audio_async(text, voice_id, rate, volume))
            
            if error:
                logger.error(f"Error in audio generation: {error}")
                return None
                
            return audio_bytes
                
        except Exception as e:
            logger.error(f"Error generating Edge TTS audio: {e}")
            return None
    
    def get_pricing_info(self):
        """Edge TTS is free, so no pricing info needed"""
        return {"free": True, "description": "Microsoft Edge TTS is free to use."}

class MacOSTTSEngine:
    """macOS System TTS Engine integration"""
    
    def __init__(self):
        self.available = platform.system() == "Darwin"
        # Define all Slavic and British voices
        self.recommended_voices = {
            "cs_CZ": ["Zuzana"],            # Czech
            "sk_SK": ["Laura"],             # Slovak
            "pl_PL": ["Zosia", "Ewa", "Jacek"], # Polish
            "ru_RU": ["Milena", "Yuri"],    # Russian
            "bg_BG": ["Daria"],             # Bulgarian
            "sl_SI": ["Vida"],              # Slovenian
            "sr_RS": ["Milena"],            # Serbian
            "uk_UA": ["Lesya"],             # Ukrainian
            "mk_MK": ["Damyan"],            # Macedonian
            "hr_HR": ["Luka"],              # Croatian
            "en_GB": ["Daniel", "Kate", "Serena"] # British
        }
    
    def get_voices(self):
        """Get available voices from macOS TTS, focusing on specified Slavic and British voices"""
        if not self.available:
            return {"Not Available": "macOS TTS is only available on macOS"}
        
        try:
            # Use macOS 'say' command to list available voices
            result = subprocess.run(['say', '-v', '?'], capture_output=True, text=True)
            
            # Maps for language codes to flags and display names
            flags = {
                "cs_CZ": "🇨🇿",  # Czech
                "sk_SK": "🇸🇰",  # Slovak
                "pl_PL": "🇵🇱",  # Polish
                "ru_RU": "🇷🇺",  # Russian
                "bg_BG": "🇧🇬",  # Bulgarian
                "sl_SI": "🇸🇮",  # Slovenian
                "sr_RS": "🇷🇸",  # Serbian
                "uk_UA": "🇺🇦",  # Ukrainian
                "mk_MK": "🇲🇰",  # Macedonian
                "hr_HR": "🇭🇷",  # Croatian
                "en_GB": "🇬🇧",  # British English
            }
            
            language_names = {
                "cs_CZ": "Czech",
                "sk_SK": "Slovak",
                "pl_PL": "Polish",
                "ru_RU": "Russian",
                "bg_BG": "Bulgarian",
                "sl_SI": "Slovenian",
                "sr_RS": "Serbian",
                "uk_UA": "Ukrainian",
                "mk_MK": "Macedonian",
                "hr_HR": "Croatian",
                "en_GB": "British",
            }
            
            # Dictionary to store recommended voices by category
            slavic_voices = {}
            british_voices = {}
            
            # Process the voice list
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 2:
                    voice_name = parts[0]
                    language = parts[-1].strip('()')
                    language_upper = language.upper()
                    
                    # Check for specific languages and include all Slavic voices
                    if any(lang.upper().replace("_", "-") in language_upper or 
                           lang.upper().replace("-", "_") in language_upper 
                           for lang in list(flags.keys())[:-1]):  # All except British
                        # Find the matching language code
                        for lang_code in flags.keys():
                            if (lang_code.upper().replace("_", "-") in language_upper or 
                                lang_code.upper().replace("-", "_") in language_upper):
                                flag = flags.get(lang_code, "")
                                lang_name = language_names.get(lang_code, language_upper)
                                display_name = f"{voice_name} ({lang_name}) {flag}"
                                slavic_voices[display_name] = voice_name
                                break
                    
                    # Also check explicitly for recommended voices
                    for lang_code, names in self.recommended_voices.items():
                        lang_variant1 = lang_code.upper().replace("_", "-")
                        lang_variant2 = lang_code.upper()
                        if (lang_variant1 in language_upper or lang_variant2 in language_upper) and voice_name in names:
                            flag = flags.get(lang_code, "")
                            lang_name = language_names.get(lang_code, language_upper)
                            display_name = f"{voice_name} ({lang_name}) {flag}"
                            
                            if lang_code == "en_GB":
                                british_voices[display_name] = voice_name
                            else:
                                slavic_voices[display_name] = voice_name
                    
                    # Also add any British voice
                    if "EN_GB" in language_upper or "EN-GB" in language_upper:
                        british_voices[f"{voice_name} (British) 🇬🇧"] = voice_name
            
            # Create the final voice dictionary
            voices = {}
            
            # Add a heading for Slavic voices if any were found
            if slavic_voices:
                voices["--- SLAVIC VOICES ---"] = "heading_separator"
                voices.update(slavic_voices)
            
            # Add British voices if any
            if british_voices:
                voices["--- BRITISH VOICES ---"] = "heading_separator"
                voices.update(british_voices)
                
            # Add a fallback option if no voices were found
            if not voices:
                voices["No Recommended Voices Found"] = "default"
                
            return voices
        except Exception as e:
            logger.error(f"Exception fetching macOS voices: {e}")
            return {"Error": str(e)}
    
    def test_connectivity(self):
        """Test if macOS TTS is available"""
        if not self.available:
            return False
        
        try:
            result = subprocess.run(['say', '--version'], capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False
    
    def generate_audio_for_testing(self, text, voice_id):
        """Generate audio for testing purposes"""
        audio_file = self._generate_audio_to_file(text, voice_id)
        return audio_file
    
    def _generate_audio_to_file(self, text, voice_id):
        """Generate audio to a temporary file and return the path"""
        if not self.available or voice_id == "Not Available":
            return None
        
        try:
            # Create temp file for output
            with tempfile.NamedTemporaryFile(delete=False, suffix=".aiff") as temp_file:
                output_path = temp_file.name
            
            # Use macOS 'say' command to generate audio
            max_retries = 3
            
            for attempt in range(max_retries):
                try:
                    subprocess.run([
                        'say',
                        '-v', voice_id,
                        '-o', output_path,
                        text
                    ], check=True, timeout=60)
                    break  # Success
                except subprocess.SubprocessError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"macOS TTS failed on attempt {attempt+1}/{max_retries}: {e}")
                        time.sleep(1)
                    else:
                        logger.error(f"All macOS TTS attempts failed: {e}")
                        return None
            
            # Convert to MP3 if ffmpeg is available (for better compatibility with Streamlit)
            try:
                mp3_path = output_path.replace('.aiff', '.mp3')
                subprocess.run([
                    'ffmpeg',
                    '-y',
                    '-i', output_path,
                    '-f', 'mp3',
                    mp3_path
                ], check=True, capture_output=True)
                
                # If conversion succeeded, use MP3 instead
                if os.path.exists(mp3_path):
                    os.unlink(output_path)
                    return mp3_path
            except Exception:
                # Fallback to AIFF if conversion fails
                pass
                
            return output_path
                
        except Exception as e:
            logger.error(f"Exception generating macOS TTS audio: {e}")
            return None
    
    def generate_audio(self, text, voice_id):
        """Generate audio bytes directly - to be used for both testing and production"""
        if not self.available or voice_id == "Not Available":
            return None
        
        try:
            # Generate audio to file
            temp_file = self._generate_audio_to_file(text, voice_id)
            
            if not temp_file or not os.path.exists(temp_file):
                return None
                
            # Read file content
            with open(temp_file, 'rb') as f:
                audio_data = f.read()
                
            # Clean up
            os.unlink(temp_file)
            
            return audio_data
            
        except Exception as e:
            logger.error(f"Exception generating macOS TTS audio bytes: {e}")
            return None
    
    def get_pricing_info(self):
        """macOS TTS is free, so no pricing info needed"""
        return {"free": True, "description": "macOS System TTS is free to use on Apple devices"}

class ElevenLabsEngine:
    """ElevenLabs TTS Engine integration with updated implementation"""
    
    def __init__(self):
        self.base_url = "https://api.elevenlabs.io/v1"
        self.api_key = os.getenv("ELEVENLABS_API_KEY") or None
        self.client = None
        self.initialize_client()
    
    def initialize_client(self):
        """Initialize ElevenLabs client if SDK is available"""
        if not HAS_ELEVENLABS or not self.api_key:
            return
            
        try:
            self.client = ElevenLabs(api_key=self.api_key)
        except Exception as e:
            logger.error(f"Failed to initialize ElevenLabs client: {e}")
            self.client = None
    
    def set_api_key(self, api_key):
        """Set the API key for ElevenLabs"""
        self.api_key = api_key
        self.initialize_client()
    
    def get_voices(self):
        """Get available voices from ElevenLabs, focusing on Croatian and British voices"""
        if not self.api_key:
            return {"No API Key": "Please add your ElevenLabs API key"}
        
        if self.client:
            # Using SDK
            try:
                response = self.client.voices.search()
                voices = {}
                croatian_voices = {}
                british_voices = {}
                
                for voice in response.voices:
                    # Check if voice is Croatian or British
                    voice_labels = {label.name: label.value for label in getattr(voice, 'labels', [])}
                    is_croatian = voice_labels.get('language') == 'croatian'
                    is_british = voice_labels.get('language') == 'english' and voice_labels.get('accent') == 'british'
                    is_cloned = getattr(voice, 'category', '') == 'cloned'
                    
                    display_name = voice.name
                    if is_croatian:
                        display_name += " 🇭🇷"
                        croatian_voices[display_name] = voice.voice_id
                    elif is_british:
                        display_name += " 🇬🇧"
                        british_voices[display_name] = voice.voice_id
                    
                    if is_cloned:
                        display_name += " (Cloned)"
                
                # Create combined dictionary with headers
                voices["--- CROATIAN VOICES ---"] = "heading_separator"
                voices.update(croatian_voices)
                voices["--- BRITISH VOICES ---"] = "heading_separator"
                voices.update(british_voices)
                
                if not croatian_voices and not british_voices:
                    voices = {"No Croatian/British Voices": "Please select another voice"}
                
                return voices
            except Exception as e:
                logger.error(f"Error getting voices with SDK: {e}")
                # Fall back to REST API
                self.client = None
        
        # Using REST API if SDK failed or not available
        try:
            headers = {"xi-api-key": self.api_key}
            response = requests.get(f"{self.base_url}/voices", headers=headers)
            
            if response.status_code == 200:
                voices_data = response.json()
                croatian_voices = {}
                british_voices = {}
                other_voices = {}
                
                for voice in voices_data.get("voices", []):
                    # Check for Croatian or British voices
                    labels = voice.get("labels", {})
                    is_croatian = any(label.get("name") == "language" and label.get("value") == "croatian" for label in labels)
                    is_british = any(label.get("name") == "language" and label.get("value") == "english" and 
                                    any(l.get("name") == "accent" and l.get("value") == "british" for l in labels))
                    is_cloned = voice.get("category") == "cloned"
                    
                    display_name = voice.get("name", "Unknown")
                    if is_croatian:
                        display_name += " 🇭🇷"
                        if is_cloned:
                            display_name += " (Cloned)"
                        croatian_voices[display_name] = voice.get("voice_id")
                    elif is_british:
                        display_name += " 🇬🇧"
                        if is_cloned:
                            display_name += " (Cloned)"
                        british_voices[display_name] = voice.get("voice_id")
                    elif len(croatian_voices) == 0 and len(british_voices) == 0:
                        # Add a few other voices as fallback
                        if len(other_voices) < 3:  # Limit to just a few
                            if is_cloned:
                                display_name += " (Cloned)"
                            other_voices[display_name] = voice.get("voice_id")
                
                # Create combined dictionary with headers
                voices = {}
                if croatian_voices:
                    voices["--- CROATIAN VOICES ---"] = "heading_separator"
                    voices.update(croatian_voices)
                if british_voices:
                    voices["--- BRITISH VOICES ---"] = "heading_separator"
                    voices.update(british_voices)
                if not croatian_voices and not british_voices:
                    voices["--- OTHER VOICES ---"] = "heading_separator"
                    voices.update(other_voices or {"No Croatian/British Voices": "Please select another voice"})
                
                return voices
            else:
                logger.error(f"Error fetching ElevenLabs voices: {response.text}")
                return {"Error": "Failed to fetch voices"}
        except Exception as e:
            logger.error(f"Exception fetching ElevenLabs voices: {e}")
            return {"Error": str(e)}
    
    def get_user_info(self):
        """Get user subscription info and remaining credits"""
        if not self.api_key:
            return None
        
        if self.client:
            try:
                user_info = self.client.user.get()
                subscription = user_info.subscription
                
                return {
                    "tier": subscription.tier,
                    "chars_remaining": subscription.character_count,
                    "chars_limit": subscription.character_limit,
                    "usage_percent": (subscription.character_limit - subscription.character_count) / subscription.character_limit * 100 if subscription.character_limit > 0 else 0
                }
            except Exception as e:
                logger.error(f"Error getting user info with SDK: {e}")
                # Fall back to REST API
                pass
        
        # Using REST API if SDK failed or not available
        try:
            headers = {"xi-api-key": self.api_key}
            response = requests.get(f"{self.base_url}/user/subscription", headers=headers)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Error fetching ElevenLabs user info: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Exception fetching ElevenLabs user info: {e}")
            return None
    
    def test_connectivity(self):
        """Test connectivity to ElevenLabs API"""
        if not self.api_key:
            return False
        
        if self.client:
            try:
                self.client.voices.get_all()
                return True
            except Exception:
                # Fall back to REST API
                pass
        
        try:
            headers = {"xi-api-key": self.api_key}
            response = requests.get(f"{self.base_url}/voices", headers=headers)
            return response.status_code == 200
        except Exception:
            return False
    
    def generate_audio_for_testing(self, text, voice_id, model_id="eleven_multilingual_v2"):
        """Generate audio for testing purposes"""
        audio_data = self.generate_audio(text, voice_id, model_id)
        if not audio_data:
            return None
            
        # Save to temp file for streamlit audio playback
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
            temp_file.write(audio_data)
            return temp_file.name
    
    def generate_audio(self, text, voice_id, model_id="eleven_multilingual_v2"):
        """Generate audio bytes directly - to be used for both testing and production"""
        if not self.api_key:
            return None
        
        # Use SDK if available
        if self.client:
            try:
                voice_settings = {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True
                }
                
                audio = self.client.text_to_speech.convert(
                    text=text,
                    voice_id=voice_id,
                    model_id=model_id,
                    voice_settings=voice_settings,
                    output_format="mp3_44100_128"
                )
                return audio
            except Exception as e:
                logger.error(f"Error generating audio with SDK: {e}")
                # Fall back to REST API
        
        # Using REST API if SDK failed or not available
        try:
            max_retries = CONNECTION_SETTINGS["max_retries"]
            retry_delay = CONNECTION_SETTINGS["retry_delay"]
            exponential_backoff = CONNECTION_SETTINGS["exponential_backoff"]
            
            headers = {
                "xi-api-key": self.api_key,
                "Content-Type": "application/json"
            }
            
            data = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75
                }
            }
            
            # Implement retry logic
            for attempt in range(max_retries + 1):
                try:
                    response = requests.post(
                        f"{self.base_url}/text-to-speech/{voice_id}/stream",
                        headers=headers,
                        json=data,
                        timeout=CONNECTION_SETTINGS["timeout"]
                    )
                    
                    if response.status_code == 200:
                        return response.content
                    else:
                        logger.error(f"ElevenLabs API error: {response.text}")
                except requests.exceptions.RequestException as e:
                    logger.warning(f"ElevenLabs request failed on attempt {attempt+1}/{max_retries+1}: {str(e)}")
                    
                    if attempt < max_retries:
                        # Calculate delay with exponential backoff
                        if exponential_backoff:
                            current_delay = retry_delay * (2 ** attempt)
                            current_delay = min(current_delay, 30)  # Cap at 30 seconds
                        else:
                            current_delay = retry_delay
                            
                        time.sleep(current_delay)
                    else:
                        return None
            
            return None  # Should not reach here but just in case
                
        except Exception as e:
            logger.error(f"Exception generating ElevenLabs audio: {e}")
            return None
    
    def get_pricing_info(self):
        """Get ElevenLabs pricing information and remaining credits"""
        user_info = self.get_user_info()
        if not user_info:
            return {"info": "Pricing information unavailable without API key"}
        
        subscription = user_info.get("subscription", {})
        tier = subscription.get("tier", "unknown")
        chars_left = subscription.get("character_count", 0)
        chars_limit = subscription.get("character_limit", 0)
        
        return {
            "tier": tier,
            "chars_remaining": chars_left,
            "chars_limit": chars_limit,
            "usage_percent": (chars_limit - chars_left) / chars_limit * 100 if chars_limit > 0 else 0,
            "description": "ElevenLabs pricing based on characters and subscription tier"
        }

class GoogleTTSEngine:
    """Google Cloud TTS Engine integration"""
    
    def __init__(self):
        self.api_key = None
    
    def set_api_key(self, api_key):
        """Set the API key for Google Cloud TTS"""
        self.api_key = api_key
    
    def get_voices(self):
        """Get available voices from Google TTS, focusing on Croatian and British voices"""
        if not self.api_key:
            return {"No API Key": "Please add your Google Cloud API key"}
        
        try:
            url = f"https://texttospeech.googleapis.com/v1/voices?key={self.api_key}"
            response = requests.get(url)
            
            if response.status_code == 200:
                voices_data = response.json()
                croatian_voices = {}
                british_voices = {}
                
                # Extract Croatian and British voices
                for voice in voices_data.get("voices", []):
                    language_codes = voice.get("languageCodes", [])
                    
                    # Croatian voices
                    if "hr-HR" in language_codes:
                        name = voice.get("name", "")
                        display_name = voice.get("name", "").replace("hr-HR-", "") + " 🇭🇷"
                        croatian_voices[display_name] = name
                    
                    # British voices
                    elif "en-GB" in language_codes:
                        name = voice.get("name", "")
                        display_name = voice.get("name", "").replace("en-GB-", "") + " 🇬🇧"
                        british_voices[display_name] = name
                
                # Create combined dictionary with headers
                voices = {}
                if croatian_voices:
                    voices["--- CROATIAN VOICES ---"] = "heading_separator"
                    voices.update(croatian_voices)
                if british_voices:
                    voices["--- BRITISH VOICES ---"] = "heading_separator"
                    voices.update(british_voices)
                
                # If no voices found, include a message
                if not croatian_voices and not british_voices:
                    voices["No Croatian/British Voices"] = "Please select another engine"
                
                return voices
            else:
                logger.error(f"Error fetching Google voices: {response.text}")
                return {"Error": "Failed to fetch voices"}
        except Exception as e:
            logger.error(f"Exception fetching Google voices: {e}")
            return {"Error": str(e)}
    
    def test_connectivity(self):
        """Test connectivity to Google Cloud TTS API"""
        if not self.api_key:
            return False
        
        try:
            url = f"https://texttospeech.googleapis.com/v1/voices?key={self.api_key}"
            response = requests.get(url)
            return response.status_code == 200
        except Exception:
            return False
    
    def generate_audio_for_testing(self, text, voice_id):
        """Generate audio for testing purposes"""
        audio_data = self.generate_audio(text, voice_id)
        if not audio_data:
            return None
            
        # Save to temp file for streamlit audio playback
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
            temp_file.write(audio_data)
            return temp_file.name
    
    def generate_audio(self, text, voice_id):
        """Generate audio bytes directly - to be used for both testing and production"""
        if not self.api_key:
            return None
        
        max_retries = CONNECTION_SETTINGS["max_retries"]
        retry_delay = CONNECTION_SETTINGS["retry_delay"]
        exponential_backoff = CONNECTION_SETTINGS["exponential_backoff"]
        
        try:
            # Determine language code from voice_id
            lang_code = "hr-HR"  # Default to Croatian
            if "en-GB" in voice_id:
                lang_code = "en-GB"
            
            url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self.api_key}"
            data = {
                "input": {"text": text},
                "voice": {"name": voice_id, "languageCode": lang_code},
                "audioConfig": {"audioEncoding": "MP3"}
            }
            
            # Implement retry logic
            for attempt in range(max_retries + 1):
                try:
                    response = requests.post(
                        url, 
                        json=data,
                        timeout=CONNECTION_SETTINGS["timeout"]
                    )
                    
                    if response.status_code == 200:
                        audio_content = response.json().get("audioContent", "")
                        return base64.b64decode(audio_content)
                    else:
                        logger.error(f"Google TTS API error: {response.text}")
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Google TTS request failed on attempt {attempt+1}/{max_retries+1}: {str(e)}")
                    
                    if attempt < max_retries:
                        # Calculate delay with exponential backoff
                        if exponential_backoff:
                            current_delay = retry_delay * (2 ** attempt)
                            current_delay = min(current_delay, 30)  # Cap at 30 seconds
                        else:
                            current_delay = retry_delay
                            
                        time.sleep(current_delay)
                    else:
                        return None
            
            return None
                
        except Exception as e:
            logger.error(f"Exception generating Google TTS audio: {e}")
            return None
    
    def get_pricing_info(self):
        """Get Google Cloud TTS pricing information"""
        return {
            "standard_voices": "$4.00 per 1 million characters",
            "wavenet_voices": "$16.00 per 1 million characters",
            "neural_voices": "$16.00 per 1 million characters",
            "description": "Google Cloud TTS pricing varies by voice type"
        }

class TTSEngineManager:
    """Manager for multiple TTS engines"""
    
    def __init__(self):
        # Create engines in the specified order: Edge, macOS, ElevenLabs, Google
        engines = {}
        
        # 1. Edge TTS
        engines["edge_tts"] = EdgeTTSEngine()
        
        # 2. macOS TTS (if available)
        if platform.system() == "Darwin":
            engines["macos_tts"] = MacOSTTSEngine()
        
        # 3. ElevenLabs
        engines["elevenlabs"] = ElevenLabsEngine()
        
        # 4. Google TTS
        engines["google_tts"] = GoogleTTSEngine()
        
        self.engines = engines
        
    def get_engine(self, engine_name):
        """Get the specified TTS engine"""
        return self.engines.get(engine_name)
    
    def get_available_engines(self):
        """Get list of available engine names"""
        return list(self.engines.keys())
    
    def test_all_engines(self):
        """Test connectivity to all available engines"""
        results = {}
        for name, engine in self.engines.items():
            results[name] = engine.test_connectivity()
        return results

#################################################
# UI Components for TTS Engine Selection
#################################################

def render_tts_engine_selector(tts_manager):
    """Render the TTS engine selection UI"""
    
    st.sidebar.header("TTS Engine Selection")
    
    # Get available engines
    available_engines = tts_manager.get_available_engines()
    engine_names = {
        "edge_tts": "Microsoft Edge TTS",
        "macos_tts": "macOS System TTS",
        "elevenlabs": "ElevenLabs TTS",
        "google_tts": "Google Cloud TTS"
    }
    
    # Create display names for available engines
    display_names = [engine_names.get(name, name) for name in available_engines]
    
    # Set default index to Edge TTS if available
    default_index = 0
    if "edge_tts" in available_engines:
        default_index = available_engines.index("edge_tts")
    
    # Engine selection
    selected_index = st.sidebar.selectbox(
        "Select TTS Engine:",
        range(len(display_names)),
        format_func=lambda i: display_names[i],
        index=default_index
    )
    
    selected_engine_id = available_engines[selected_index]
    selected_engine = tts_manager.get_engine(selected_engine_id)
    
    # Return the selected engine and its ID
    return selected_engine, selected_engine_id

def render_engine_config(engine, engine_id):
    """Render configuration UI for the selected engine"""
    
    st.sidebar.subheader("Engine Configuration")
    
    if engine_id == "elevenlabs":
        # ElevenLabs API key input
        api_key = st.sidebar.text_input(
            "ElevenLabs API Key:",
            value=os.getenv("ELEVENLABS_API_KEY", ""),
            type="password",
            help="Enter your ElevenLabs API key"
        )
        engine.set_api_key(api_key)
        
        # Show subscription info if API key is provided
        if api_key:
            pricing_info = engine.get_pricing_info()
            
            # Display in an expander
            with st.sidebar.expander("ElevenLabs Subscription Info", expanded=True):
                if isinstance(pricing_info, dict) and "tier" in pricing_info:
                    st.write(f"**Subscription Tier:** {pricing_info['tier']}")
                    st.write(f"**Characters Remaining:** {pricing_info['chars_remaining']:,}")
                    st.write(f"**Character Limit:** {pricing_info['chars_limit']:,}")
                    
                    # Show usage bar
                    st.progress(pricing_info['usage_percent'] / 100)
                    st.write(f"Usage: {pricing_info['usage_percent']:.1f}%")
                else:
                    st.warning("Could not fetch subscription information.")
    
    elif engine_id == "google_tts":
        # Google Cloud API key input
        api_key = st.sidebar.text_input(
            "Google Cloud API Key:",
            value=os.getenv("GOOGLE_API_KEY", ""),
            type="password",
            help="Enter your Google Cloud API key with Text-to-Speech enabled"
        )
        engine.set_api_key(api_key)
        
        # Show pricing info
        with st.sidebar.expander("Google TTS Pricing Info"):
            pricing_info = engine.get_pricing_info()
            st.write(f"**Standard Voices:** {pricing_info['standard_voices']}")
            st.write(f"**WaveNet Voices:** {pricing_info['wavenet_voices']}")
            st.write(f"**Neural Voices:** {pricing_info['neural_voices']}")
    
    # Return any config data needed
    return {
        "configured": True  # Basic flag to indicate configuration is done
    }

def render_voice_selector(engine, engine_id):
    """Render voice selection UI for the selected engine"""
    
    st.sidebar.subheader("Voice Selection")
    
    # Get available voices
    voices = engine.get_voices()
    
    if not voices:
        st.sidebar.warning("No voices available.")
        return None
    
    # Remove heading separators for selectbox
    voice_names = [k for k in voices.keys() if voices[k] != "heading_separator"]
    
    # If there are headings, display section titles
    if any(v == "heading_separator" for v in voices.values()):
        for heading in [k for k in voices.keys() if voices[k] == "heading_separator"]:
            st.sidebar.markdown(f"**{heading}**")
    
    # Set default to Srećko if available
    default_index = 0
    for i, name in enumerate(voice_names):
        if "Srećko" in name:
            default_index = i
            break
    
    selected_voice_name = st.sidebar.selectbox(
        "Select Voice:",
        voice_names,
        index=default_index,
        help="Select a voice for speech synthesis"
    )
    
    # Get the actual voice ID
    selected_voice_id = voices[selected_voice_name]
    
    return {
        "name": selected_voice_name,
        "id": selected_voice_id
    }

def render_test_section(engine, engine_id, voice_data):
    """Render the TTS testing section"""
    
    st.sidebar.subheader("Test TTS")
    
    # Text input for testing
    test_text = st.sidebar.text_input(
        "Test Sentence:",
        value="Dobar dan! Kako ste danas?",
        help="Enter some text to test the selected voice"
    )
    
    # Additional parameters based on engine
    params = {}
    
    if engine_id == "edge_tts":
        rate = st.sidebar.slider("Test Speech Rate", -50, 50, 0, 5, key="test_rate", help="Adjust speech speed")
        volume = st.sidebar.slider("Test Volume", -50, 50, 0, 5, key="test_volume", help="Adjust volume")
        params["rate"] = f"{rate:+d}%" if rate != 0 else "+0%"
        params["volume"] = f"{volume:+d}%" if volume != 0 else "+0%"
    
    # Test button
    if st.sidebar.button("Test Voice"):
        with st.sidebar:
            with st.spinner("Generating test audio..."):
                if engine_id == "edge_tts":
                    audio_file = engine.generate_audio_for_testing(
                        test_text, 
                        voice_data["id"], 
                        rate=params.get("rate", "+0%"), 
                        volume=params.get("volume", "+0%")
                    )
                else:
                    audio_file = engine.generate_audio_for_testing(
                        test_text, 
                        voice_data["id"]
                    )
                
                if audio_file and os.path.exists(audio_file):
                    st.audio(audio_file)
                    
                    # Offer to save the test file
                    if st.download_button(
                        "Save Test Audio",
                        data=open(audio_file, "rb").read(),
                        file_name=f"test_{engine_id}_{voice_data['name'].replace(' ', '_')}.mp3",
                        mime="audio/mp3"
                    ):
                        st.success("Test audio saved!")
                else:
                    st.error("Failed to generate test audio")

#################################################
# SRT Processing Functions
#################################################

def parse_srt_timecode(timecode_str):
    """Parse SRT timecode to frame number (30fps)"""
    pattern = r'(\d{2}):(\d{2}):(\d{2}):(\d{2})'
    match = re.match(pattern, timecode_str)
    if not match:
        raise ValueError(f"Invalid timecode format: {timecode_str}")
    
    hours, minutes, seconds, frames = map(int, match.groups())
    total_frames = (hours * 3600 + minutes * 60 + seconds) * 30 + frames
    return total_frames

def frames_to_samples(frames, sample_rate=48000, fps=30):
    """Convert frame number to audio sample position"""
    seconds = frames / fps
    return int(seconds * sample_rate)

def parse_srt_file(srt_content):
    """Parse SRT file content and extract subtitles with timecodes"""
    subtitles = []
    blocks = re.split(r'\n\s*\n', srt_content.strip())
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 3:
            subtitle_num = lines[0]
            timecode_line = lines[1]
            text_lines = lines[2:]
            
            # Parse timecode line
            timecode_match = re.match(r'(\d{2}:\d{2}:\d{2}:\d{2})\s*-->\s*(\d{2}:\d{2}:\d{2}:\d{2})', timecode_line)
            if timecode_match:
                start_tc, end_tc = timecode_match.groups()
                start_frame = parse_srt_timecode(start_tc)
                end_frame = parse_srt_timecode(end_tc)
                text = ' '.join(text_lines)
                
                subtitles.append({
                    'number': int(subtitle_num),
                    'start_frame': start_frame,
                    'end_frame': end_frame,
                    'start_timecode': start_tc,
                    'end_timecode': end_tc,
                    'text': text
                })
    
    return subtitles

def create_broadcast_wave_header(total_samples, sample_rate=48000, channels=1, bit_depth=24):
    """Create a broadcast wave file header with BWF metadata"""
    
    # Calculate sizes
    bytes_per_sample = bit_depth // 8
    block_align = channels * bytes_per_sample
    byte_rate = sample_rate * channels * bytes_per_sample
    data_size = total_samples * block_align
    
    # BWF specific data
    bext_chunk_size = 602  # Standard BEXT chunk size
    total_file_size = 36 + bext_chunk_size + 8 + data_size  # RIFF header + BEXT + data chunk
    
    # Create RIFF header
    header = b'RIFF'
    header += struct.pack('<I', total_file_size - 8)
    header += b'WAVE'
    
    # Create fmt chunk
    header += b'fmt '
    header += struct.pack('<I', 16)  # fmt chunk size
    header += struct.pack('<H', 1)   # PCM format
    header += struct.pack('<H', channels)
    header += struct.pack('<I', sample_rate)
    header += struct.pack('<I', byte_rate)
    header += struct.pack('<H', block_align)
    header += struct.pack('<H', bit_depth)
    
    # Create BEXT chunk (Broadcast Wave Extension)
    header += b'bext'
    header += struct.pack('<I', bext_chunk_size)
    
    # BEXT data fields
    description = b'TTS Generated Audio'.ljust(256, b'\x00')
    originator = b'TTS Streamlit App'.ljust(32, b'\x00')
    originator_ref = b'TTS_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S').encode().ljust(32, b'\x00')
    origination_date = datetime.datetime.now().strftime('%Y-%m-%d').encode().ljust(10, b'\x00')
    origination_time = datetime.datetime.now().strftime('%H:%M:%S').encode().ljust(8, b'\x00')
    time_reference_low = struct.pack('<I', 0)  # Sample accurate start time
    time_reference_high = struct.pack('<I', 0)
    version = struct.pack('<H', 1)
    umid = b'\x00' * 64  # Unique Material Identifier
    loudness_value = struct.pack('<h', 0)
    loudness_range = struct.pack('<h', 0)
    max_true_peak_level = struct.pack('<h', 0)
    max_momentary_loudness = struct.pack('<h', 0)
    max_short_term_loudness = struct.pack('<h', 0)
    reserved = b'\x00' * 180
    channel_str = "mono" if channels == 1 else "stereo"
    coding_history = f"A=PCM,F={sample_rate},W={bit_depth},M={channel_str},T=TTS\r\n".encode().ljust(256, b'\x00')
    
    header += description + originator + originator_ref + origination_date + origination_time
    header += time_reference_low + time_reference_high + version + umid
    header += loudness_value + loudness_range + max_true_peak_level
    header += max_momentary_loudness + max_short_term_loudness + reserved + coding_history
    
    # Create data chunk header
    header += b'data'
    header += struct.pack('<I', data_size)
    
    return header

def get_cache_key(text, engine_id, voice_id, params=None):
    """Generate a unique cache key for TTS text and parameters"""
    if params is None:
        params = {}
        
    param_str = "|".join(f"{k}={v}" for k, v in sorted(params.items()))
    data = f"{text}|{engine_id}|{voice_id}|{param_str}"
    key = hashlib.md5(data.encode('utf-8')).hexdigest()
    return key

def get_cached_audio(text, engine_id, voice_id, params=None):
    """Check if audio is cached and return it if found"""
    if not CONNECTION_SETTINGS["use_cache"]:
        return None
        
    cache_key = get_cache_key(text, engine_id, voice_id, params)
    cache_file = CACHE_DIR / f"{cache_key}.mp3"
    
    if cache_file.exists():
        try:
            with open(cache_file, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read cache file: {e}")
    
    return None

def save_to_cache(text, engine_id, voice_id, audio_data, params=None):
    """Save generated audio to cache"""
    if not CONNECTION_SETTINGS["use_cache"] or audio_data is None:
        return
        
    cache_key = get_cache_key(text, engine_id, voice_id, params)
    cache_file = CACHE_DIR / f"{cache_key}.mp3"
    
    try:
        with open(cache_file, 'wb') as f:
            f.write(audio_data)
    except Exception as e:
        logger.warning(f"Failed to write to cache: {e}")

def convert_audio_format(audio_data, sample_rate=48000, target_channels=1, target_bit_depth=24):
    """Convert audio data to required format using ffmpeg, with resilient error handling"""
    import subprocess
    
    # Create temporary files
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_in:
        temp_in_path = temp_in.name
        temp_in.write(audio_data)
    
    temp_out_path = temp_in_path.replace('.mp3', '.wav')
    
    try:
        # Run ffmpeg with retry logic for conversion issues
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                result = subprocess.run([
                    'ffmpeg', '-y', 
                    '-i', temp_in_path,
                    '-ar', str(sample_rate),
                    '-ac', str(target_channels),
                    '-sample_fmt', 's32' if target_bit_depth == 32 else 's24le' if target_bit_depth == 24 else 's16',
                    '-loglevel', 'error',  # Only show errors
                    temp_out_path
                ], check=True, capture_output=True, timeout=30)
                break  # Success
            except subprocess.SubprocessError as e:
                if attempt < max_attempts - 1:
                    logger.warning(f"FFmpeg conversion failed, attempt {attempt+1}/{max_attempts}: {e}")
                    time.sleep(1)  # Short delay before retry
                else:
                    raise RuntimeError(f"Audio conversion failed after {max_attempts} attempts: {e}")
        
        # Read the converted audio
        with wave.open(temp_out_path, 'rb') as wav_in:
            audio_samples = wav_in.readframes(wav_in.getnframes())
        
        return audio_samples
        
    finally:
        # Clean up temporary files
        for file_path in [temp_in_path, temp_out_path]:
            if os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except Exception:
                    pass

def write_audio_to_position(wav_file_path, audio_data, start_sample, sample_rate=48000, 
                           target_channels=1, target_bit_depth=24, lock=None):
    """Write audio data to specific position in the BWF file with thread-safety"""
    try:
        # Convert the audio format
        audio_samples = convert_audio_format(audio_data, sample_rate, target_channels, target_bit_depth)
        
        # Calculate byte position in the target file
        bytes_per_sample = target_bit_depth // 8
        header_size = 8 + 602 + 36 + 8  # RIFF + bext + fmt + data headers
        byte_position = header_size + (start_sample * target_channels * bytes_per_sample)
        
        # Write to target file with optional locking for thread safety
        if lock:
            lock.acquire()
            
        try:
            with open(wav_file_path, 'r+b') as wav_file:
                wav_file.seek(byte_position)
                wav_file.write(audio_samples)
        finally:
            if lock and lock.locked():
                lock.release()
                
        return True
    except Exception as e:
        logger.error(f"Error writing audio to BWF file: {e}")
        return False

def optimize_subtitle_processing_order(subtitles, batch_size=5):
    """Optimize the order of subtitle processing to prioritize evenly spaced frames"""
    if len(subtitles) <= batch_size:
        return subtitles
    
    # Create batches spread throughout the timeline for better progressive playback
    batches = []
    stride = max(len(subtitles) // batch_size, 1)
    
    for i in range(stride):
        batch = []
        for j in range(i, len(subtitles), stride):
            batch.append(subtitles[j])
        batches.append(batch)
        
    # Flatten the batches
    optimized_order = []
    for batch in batches:
        optimized_order.extend(batch)
        
    # Ensure we haven't missed any subtitles
    included_nums = {sub['number'] for sub in optimized_order}
    for sub in subtitles:
        if sub['number'] not in included_nums:
            optimized_order.append(sub)
            
    return optimized_order

def process_subtitle_with_selected_engine(subtitle, engine, engine_id, voice_id, output_path, 
                                         sample_rate, channels, bit_depth, status_callbacks, 
                                         file_lock, connection_settings, rate="+0%", volume="+0%"):
    """Process a subtitle using the selected TTS engine"""
    subtitle_text = subtitle['text']
    subtitle_num = subtitle['number']
    
    # Update status
    if status_callbacks.get('update_status'):
        status_callbacks['update_status'](f"Processing subtitle {subtitle_num} with {engine_id}: {subtitle_text[:50]}...")
    
    # Prepare params for cache key
    params = {}
    if engine_id == "edge_tts":
        params = {"rate": rate, "volume": volume}
        
    # Check cache first
    cached_audio = get_cached_audio(subtitle_text, engine_id, voice_id, params)
    if cached_audio is not None:
        if status_callbacks.get('update_status'):
            status_callbacks['update_status'](f"Using cached audio for subtitle {subtitle_num}")
        
        # Calculate start position in samples
        start_sample = frames_to_samples(subtitle['start_frame'], sample_rate)
        
        # Write audio to the BWF file at the correct position
        success = write_audio_to_position(
            str(output_path), 
            cached_audio, 
            start_sample, 
            sample_rate, 
            channels, 
            bit_depth,
            file_lock
        )
        
        return success, False
    
    try:
        # Generate audio based on the selected engine
        if engine_id == "edge_tts":
            # Use optimized Edge TTS implementation
            audio_data = engine.generate_audio(subtitle_text, voice_id, rate, volume)
        elif engine_id == "elevenlabs":
            # Use ElevenLabs
            audio_data = engine.generate_audio(subtitle_text, voice_id)
        elif engine_id == "google_tts":
            # Use Google Cloud TTS
            audio_data = engine.generate_audio(subtitle_text, voice_id)
        elif engine_id == "macos_tts":
            # Use macOS system TTS
            audio_data = engine.generate_audio(subtitle_text, voice_id)
        else:
            raise Exception(f"Unsupported TTS engine: {engine_id}")
        
        if audio_data is None:
            raise Exception(f"Failed to generate audio with {engine_id}")
        
        # Save to cache
        save_to_cache(subtitle_text, engine_id, voice_id, audio_data, params)
        
        # Calculate start position in samples
        start_sample = frames_to_samples(subtitle['start_frame'], sample_rate)
        
        # Write audio to the BWF file at the correct position
        success = write_audio_to_position(
            str(output_path), 
            audio_data, 
            start_sample, 
            sample_rate, 
            channels, 
            bit_depth,
            file_lock
        )
        
        return success, False  # success, used_fallback
        
    except Exception as e:
        logger.error(f"Error processing subtitle {subtitle_num}: {e}")
        if status_callbacks.get('update_status'):
            status_callbacks['update_status'](f"⚠️ Failed to generate audio for subtitle {subtitle_num}: {str(e)}")
        return False, False

def process_subtitle_batch(subtitle_batch, engine, engine_id, voice_id, output_path, sample_rate, 
                          channels, bit_depth, status_callbacks, file_lock, connection_settings, rate="+0%", volume="+0%"):
    """Process a batch of subtitles in parallel and write to the BWF file"""
    results = []
    
    with ThreadPoolExecutor(max_workers=min(len(subtitle_batch), 3)) as executor:
        # Submit all tasks
        future_to_subtitle = {
            executor.submit(
                process_subtitle_with_selected_engine, 
                subtitle, 
                engine, 
                engine_id, 
                voice_id, 
                output_path, 
                sample_rate, 
                channels, 
                bit_depth, 
                status_callbacks,
                file_lock,
                connection_settings,
                rate,
                volume
            ): subtitle for subtitle in subtitle_batch
        }
        
        # Process results as they complete
        for future in as_completed(future_to_subtitle):
            subtitle = future_to_subtitle[future]
            try:
                success, used_fallback = future.result()
                results.append((subtitle, success, used_fallback))
            except Exception as e:
                logger.error(f"Error processing subtitle {subtitle['number']}: {e}")
                results.append((subtitle, False, False))
    
    return results

#################################################
# Simple TTS Functions
#################################################

def generate_simple_tts_audio(text, engine, engine_id, voice_id, rate="+0%", volume="+0%"):
    """Generate TTS audio for plain text without SRT"""
    try:
        # Check cache first
        params = {}
        if engine_id == "edge_tts":
            params = {"rate": rate, "volume": volume}
            
        cached_audio = get_cached_audio(text, engine_id, voice_id, params)
        if cached_audio is not None:
            return cached_audio
            
        # Generate based on selected engine using same method as production
        if engine_id == "edge_tts":
            audio_data = engine.generate_audio(text, voice_id, rate, volume)
        else:
            audio_data = engine.generate_audio(text, voice_id)
            
        # Save to cache
        save_to_cache(text, engine_id, voice_id, audio_data, params)
        
        return audio_data
    except Exception as e:
        logger.error(f"Error generating simple TTS audio: {e}")
        return None

#################################################
# Main Application
#################################################

def main():
    st.set_page_config(
        page_title="Croatian & British TTS Converter",
        page_icon="🎙️",
        layout="wide"
    )
    
    # Initialize TTS Engine Manager
    tts_manager = TTSEngineManager()
    
    # Create tabs for different functionality
    tab1, tab2 = st.tabs(["SRT to BWF Conversion", "Simple Text to Speech"])
    
    # Render engine selector UI in the sidebar (common for both tabs)
    selected_engine, selected_engine_id = render_tts_engine_selector(tts_manager)
    
    # Render engine configuration UI 
    engine_config = render_engine_config(selected_engine, selected_engine_id)
    
    # Render voice selector UI
    voice_data = render_voice_selector(selected_engine, selected_engine_id)
    
    # Render test section
    if voice_data:
        render_test_section(selected_engine, selected_engine_id, voice_data)
    
    # Clear cache button
    if st.sidebar.button("Clear TTS Cache"):
        cache_files = list(CACHE_DIR.glob("*.mp3")) + list(CACHE_DIR.glob("*.wav"))
        count = len(cache_files)
        for f in cache_files:
            f.unlink()
        st.sidebar.success(f"Cleared {count} cached audio files")
    
    # Create output directory
    output_dir = Path("output_wav")
    output_dir.mkdir(exist_ok=True)
    
    with tab1:
        st.title("🎙️ SRT to BWF Converter")
        st.markdown("Convert SRT subtitles to Broadcast Wave Format audio using multiple TTS engines")
        
        # Audio format settings
        st.sidebar.subheader("BWF Audio Format")
        sample_rate = st.sidebar.selectbox("Sample Rate", [44100, 48000], index=1)
        bit_depth = st.sidebar.selectbox("Bit Depth", [16, 24, 32], index=1)
        channels = st.sidebar.selectbox("Channels", [1, 2], index=0)  # Default to mono
        
        # Connection optimization settings
        st.sidebar.subheader("Connection Settings")
        with st.sidebar.expander("Processing Optimization", expanded=True):
            timeout = st.number_input(
                "Connection Timeout (seconds)",
                min_value=10,
                max_value=120,
                value=CONNECTION_SETTINGS["timeout"],
                help="Longer timeout for slow connections"
            )
            
            max_retries = st.number_input(
                "Max Retry Attempts",
                min_value=1,
                max_value=10,
                value=CONNECTION_SETTINGS["max_retries"],
                help="Higher number of retries for unstable connections"
            )
            
            retry_delay = st.number_input(
                "Initial Retry Delay (seconds)",
                min_value=1,
                max_value=10,
                value=CONNECTION_SETTINGS["retry_delay"],
                help="Delay between retry attempts"
            )
            
            exponential_backoff = st.checkbox(
                "Use Exponential Backoff",
                value=CONNECTION_SETTINGS["exponential_backoff"],
                help="Progressively increase delay between retries"
            )
            
            use_cache = st.checkbox(
                "Cache Generated Audio",
                value=CONNECTION_SETTINGS["use_cache"],
                help="Save generated audio to avoid re-downloading (recommended for slow connections)"
            )
            
            batch_size = st.number_input(
                "Batch Size",
                min_value=1,
                max_value=10,
                value=CONNECTION_SETTINGS["batch_size"],
                help="Number of subtitles to process in parallel (lower for slower connections)"
            )
        
        # Update connection settings
        CONNECTION_SETTINGS.update({
            "timeout": timeout,
            "max_retries": max_retries,
            "retry_delay": retry_delay,
            "batch_size": batch_size,
            "use_cache": use_cache,
            "exponential_backoff": exponential_backoff
        })
        
        # File upload
        st.header("Upload SRT File")
        uploaded_file = st.file_uploader(
            "Choose an SRT file with 30fps timecode format (HH:MM:SS:FF)",
            type=['srt'],
            help="Upload an SRT file where timecode is in HH:MM:SS:FF format (30fps)"
        )
        
        if uploaded_file is not None:
            # Read and parse SRT file
            srt_content = uploaded_file.read().decode('utf-8')
            
            try:
                subtitles = parse_srt_file(srt_content)
                
                if not subtitles:
                    st.error("No valid subtitles found in the file.")
                    st.stop()
                
                st.success(f"Successfully parsed {len(subtitles)} subtitles")
                
                # Display preview
                with st.expander("Preview Subtitles", expanded=False):
                    for sub in subtitles[:5]:  # Show first 5
                        st.write(f"**{sub['number']}**: {sub['start_timecode']} → {sub['end_timecode']}")
                        st.write(f"Text: {sub['text']}")
                        st.write("---")
                    if len(subtitles) > 5:
                        st.write(f"... and {len(subtitles) - 5} more")
                
                # Calculate total duration
                max_end_frame = max(sub['end_frame'] for sub in subtitles)
                total_duration_seconds = max_end_frame / 30
                total_samples = int(total_duration_seconds * sample_rate)
                
                st.info(f"Total duration: {total_duration_seconds:.2f} seconds ({max_end_frame} frames at 30fps)")
                st.info(f"Output file will be: {total_samples:,} samples at {sample_rate}Hz")
                
                # Generate button - only if voice is selected
                if not voice_data:
                    st.warning("Please select a voice before generating audio")
                else:
                    # Additional parameters based on engine
                    rate = "+0%"
                    volume = "+0%"
                    if selected_engine_id == "edge_tts":
                        st.subheader("Edge TTS Settings")
                        col1, col2 = st.columns(2)
                        with col1:
                            rate_val = st.slider("Speech Rate", -50, 50, 0, 5, key="srt_rate", help="Adjust speech speed")
                            rate = f"{rate_val:+d}%" if rate_val != 0 else "+0%"
                        with col2:
                            volume_val = st.slider("Volume", -50, 50, 0, 5, key="srt_volume", help="Adjust volume")
                            volume = f"{volume_val:+d}%" if volume_val != 0 else "+0%"
                    
                    if st.button("🎵 Generate Audio", type="primary"):
                        
                        # Create output filename with engine name
                        original_name = uploaded_file.name.rsplit('.', 1)[0]
                        engine_display_name = selected_engine_id.replace('_', '')
                        output_filename = f"{original_name}_{engine_display_name}_{voice_data['name'].replace(' ', '_')}_{sample_rate}Hz_{bit_depth}bit_{channels}ch.wav"
                        output_path = output_dir / output_filename
                        
                        # Progress tracking
                        progress_bar = st.progress(0)
                        status_container = st.empty()
                        metrics_container = st.container()
                        
                        with metrics_container:
                            cols = st.columns(3)
                            processed_metric = cols[0].empty()
                            success_metric = cols[1].empty()
                            error_metric = cols[2].empty()
                        
                        # Additional details container
                        details_container = st.expander("Processing Details", expanded=True)
                        
                        try:
                            # Step 1: Create empty BWF file
                            status_container.info("Creating empty broadcast wave file...")
                            
                            header = create_broadcast_wave_header(
                                total_samples, sample_rate, channels, bit_depth
                            )
                            
                            # Create file with header and zero-filled audio data
                            bytes_per_sample = bit_depth // 8
                            audio_data_size = total_samples * channels * bytes_per_sample
                            
                            with open(output_path, 'wb') as f:
                                f.write(header)
                                # Write zeros for audio data in chunks for better performance
                                chunk_size = 1024 * 1024  # 1MB chunks
                                remaining = audio_data_size
                                while remaining > 0:
                                    chunk = min(chunk_size, remaining)
                                    f.write(b'\x00' * chunk)
                                    remaining -= chunk
                            
                            details_container.success(f"Empty BWF file created: {output_path}")
                            status_container.info(f"Starting audio generation using {selected_engine_id} engine...")
                            
                            # Step 2: Generate and write audio for each subtitle
                            voice_id = voice_data['id']
                            
                            # Optimize subtitle processing order for better progressive playback
                            optimized_subtitles = optimize_subtitle_processing_order(subtitles, CONNECTION_SETTINGS["batch_size"])
                            
                            # Initialize tracking metrics
                            processed_count = 0
                            success_count = 0
                            fallback_count = 0
                            error_count = 0
                            
                            # Create a file lock for thread safety
                            file_lock = threading.Lock()
                            
                            # Process subtitles in optimized batches
                            total_subtitles = len(subtitles)
                            
                            # Callbacks for status updates
                            def update_status(message):
                                status_container.info(message)
                                details_container.text(message)
                            
                            def update_metrics():
                                processed_metric.metric("Processed", f"{processed_count}/{total_subtitles}")
                                success_metric.metric("Success", success_count)
                                error_metric.metric("Errors", error_count)
                            
                            status_callbacks = {
                                'update_status': update_status
                            }
                            
                            # Initialize metrics
                            update_metrics()
                            
                            # Process in batches based on the batch_size setting
                            batch_size = CONNECTION_SETTINGS["batch_size"]
                            for i in range(0, len(optimized_subtitles), batch_size):
                                batch = optimized_subtitles[i:i+batch_size]
                                
                                status_container.info(f"Processing batch {i//batch_size + 1}/{(len(optimized_subtitles) + batch_size - 1)//batch_size}...")
                                batch_results = process_subtitle_batch(
                                    batch, selected_engine, selected_engine_id, voice_id, output_path,
                                    sample_rate, channels, bit_depth, status_callbacks,
                                    file_lock, CONNECTION_SETTINGS, rate, volume
                                )
                                
                                # Update metrics
                                for subtitle, success, used_fallback in batch_results:
                                    processed_count += 1
                                    if success:
                                        success_count += 1
                                        if used_fallback:
                                            fallback_count += 1
                                    else:
                                        error_count += 1
                                        
                                # Update progress and metrics
                                progress = processed_count / total_subtitles
                                progress_bar.progress(progress)
                                update_metrics()
                                
                                # Add a small delay to allow UI updates
                                time.sleep(0.1)
                            
                            # Complete
                            progress_bar.progress(1.0)
                            
                            # Show summary of results
                            if error_count > 0:
                                status_container.warning(f"✅ Audio generation complete with {error_count} errors.")
                                details_container.warning(f"⚠️ {error_count} subtitles failed to generate audio.")
                            else:
                                status_container.success("✅ Audio generation complete!")
                            
                            st.success(f"🎉 Successfully generated: {output_filename}")
                            st.info(f"📁 File location: {output_path.absolute()}")
                            
                            # File info
                            file_size = output_path.stat().st_size
                            st.write(f"**File size**: {file_size / (1024*1024):.1f} MB")
                            st.write(f"**Format**: {sample_rate}Hz, {bit_depth}-bit, {'Mono' if channels == 1 else 'Stereo'}")
                            st.write(f"**Engine**: {selected_engine_id}")
                            st.write(f"**Voice**: {voice_data['name']} ({voice_id})")
                            st.write(f"**Processing summary**: {success_count} successful, {error_count} failed")
                            
                            # Download button
                            with open(output_path, 'rb') as f:
                                st.download_button(
                                    label="📥 Download BWF File",
                                    data=f.read(),
                                    file_name=output_filename,
                                    mime="audio/wav"
                                )
                            
                        except Exception as e:
                            st.error(f"Error generating audio: {str(e)}")
                            st.exception(e)
            
            except Exception as e:
                st.error(f"Error parsing SRT file: {str(e)}")
                st.exception(e)
    
    with tab2:
        st.title("🔊 Simple Text to Speech")
        st.markdown("Convert plain text to speech using multiple TTS engines")
        
        # Text area for input
        st.header("Enter Text")
        col1, col2 = st.columns([1, 0.1])
        
        with col1:
            input_text = st.text_area(
                "Text to convert to speech",
                height=200,
                placeholder="Type or paste text here...",
                label_visibility="collapsed"
            )
        with col2:
            if st.button("📋 Paste", help="Paste from clipboard"):
                try:
                    # This is a placeholder - actual clipboard paste is not available in Streamlit
                    # Just a visual effect to remind users they can paste manually
                    input_text = ""
                    st.experimental_rerun()
                except:
                    pass
        
        # TTS settings
        st.subheader("TTS Settings")
        
        rate = "+0%"
        volume = "+0%"
        
        if selected_engine_id == "edge_tts":
            col1, col2 = st.columns(2)
            with col1:
                rate_val = st.slider("Speech Rate", -50, 50, 0, 5, key="simple_rate", help="Adjust speech speed")
                rate = f"{rate_val:+d}%" if rate_val != 0 else "+0%"
            with col2:
                volume_val = st.slider("Volume", -50, 50, 0, 5, key="simple_volume", help="Adjust volume")
                volume = f"{volume_val:+d}%" if volume_val != 0 else "+0%"
        
        # Generate button
        if st.button("🎵 Generate Speech", disabled=not input_text.strip() or not voice_data):
            if not voice_data:
                st.warning("Please select a voice first")
            elif not input_text.strip():
                st.warning("Please enter some text first")
            else:
                with st.spinner("Generating speech..."):
                    try:
                        if selected_engine_id == "edge_tts":
                            audio_data = generate_simple_tts_audio(
                                input_text.strip(),
                                selected_engine,
                                selected_engine_id,
                                voice_data["id"],
                                rate,
                                volume
                            )
                        else:
                            audio_data = generate_simple_tts_audio(
                                input_text.strip(),
                                selected_engine,
                                selected_engine_id,
                                voice_data["id"]
                            )
                        
                        if audio_data:
                            # Display audio player
                            st.subheader("Generated Audio")
                            st.audio(audio_data, format="audio/mp3")
                            
                            # Output filename
                            filename = f"speech_{selected_engine_id}_{voice_data['name'].replace(' ', '_')}.mp3"
                            
                            # Download button
                            st.download_button(
                                label="💾 Download MP3",
                                data=audio_data,
                                file_name=filename,
                                mime="audio/mp3"
                            )
                            
                            # Audio info
                            st.info(f"Generated using {selected_engine_id} with voice {voice_data['name']}")
                            st.success(f"Successfully generated audio ({len(input_text.strip())} characters)")
                        else:
                            st.error("❌ Failed to generate speech. Please try again.")
                    except Exception as e:
                        st.error(f"❌ Error generating speech: {str(e)}")
    
    # Instructions
    st.sidebar.markdown("---")
    with st.sidebar.expander("📋 Instructions", expanded=False):
        st.markdown("""
        ### Multiple TTS Engine Support
        
        This application supports multiple text-to-speech engines:
        
        1. **Microsoft Edge TTS**
           - Free to use
           - Good quality Croatian and British voices
           - No API key required
        
        2. **macOS System TTS** (only on Apple computers)
           - Built-in system voices
           - Free to use
           - No internet connection required
           - Support for Slavic and British voices
           
        3. **ElevenLabs TTS**
           - Professional quality voices
           - Support for Croatian and British languages
           - Supports cloned voices
           - Requires API key
           - Usage based on subscription tier
        
        4. **Google Cloud TTS**
           - High quality neural voices
           - Requires Google Cloud API key
           - Paid service with usage billing
        
        ### SRT Format Requirements
        For the SRT to BWF converter, your SRT file must use **30fps timecode format**:
        ```
        1
        00:00:00:00 --> 00:00:02:12
        Sada svi govore o tome da bi sve moglo biti moguće.
        
        2
        00:00:02:26 --> 00:00:06:06
        Ali netko možda to samo na razini ideje.
        ```
        
        **Timecode Format**: `HH:MM:SS:FF` where FF is frame number (0-29 for 30fps)
        """)

if __name__ == "__main__":
    main()