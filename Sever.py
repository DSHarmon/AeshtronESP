import datetime
import os
import socket
import threading
import time
import wave
import ollama
import struct
from loguru import logger
from ASR.ASR import VoiceRecognition  # 根据实际路径调整
from TTS.pyttxs3_TTS import TTSEngine    # 根据实际路径调整

class AeshtronServer:
    def __init__(self):
        self.host = "0.0.0.0"
        self.port = 8080
        self.sample_rate = 16000
        self.channels = 1
        self.sample_width = 2  # 16bit = 2 bytes
        self.client_timeout = 300  # 客户端超时时间（秒）
        
        # 初始化组件
        self.asr_engine = VoiceRecognition(
            model_type="sense_voice",
            sense_voice="./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx",
            tokens="./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt",
            num_threads=4,
            use_itn=True,
            provider="cpu"
        )
        self.tts_engine = TTSEngine()
        self.ollama_client = ollama.Client(host="http://localhost:11434")
        
        # 日志配置
        logger.add("./logs/server.log", rotation="10 MB")
        self.setup_directories()
        
    def setup_directories(self):
        """创建必要的目录结构"""
        os.makedirs("./temp_audio", exist_ok=True)
        os.makedirs("./logs", exist_ok=True)
        os.makedirs("./dialogue_history", exist_ok=True)

    def start_server(self):
        """启动TCP服务器"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(1)
            logger.info(f"服务器已启动，监听 {self.host}:{self.port}")

            while True:
                try:
                    client_socket, addr = server_socket.accept()
                    logger.success(f"新的客户端连接: {addr}")
                    client_handler = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, addr)
                    )
                    client_handler.start()
                except Exception as e:
                    logger.error(f"接受连接时发生错误: {e}")

    def handle_client(self, client_socket, addr):
        """处理单个客户端连接"""
        try:
            client_socket.settimeout(self.client_timeout)
            
            # 第一步：接收语音数据
            input_audio = self.receive_audio_data(client_socket)
            if not input_audio:
                logger.warning("未接收到有效音频数据")
                return

            # 第二步：语音识别
            transcript = self.speech_to_text(input_audio)
            if not transcript:
                logger.warning("语音识别失败")
                return

            # 第三步：生成对话回复
            response_text = self.generate_response(transcript)
            if not response_text:
                logger.warning("未能生成有效回复")
                return

            # 第四步：语音合成
            output_audio = self.text_to_speech(response_text)
            if not output_audio:
                logger.warning("语音合成失败")
                return

            # 第五步：发送合成音频
            self.send_audio_data(client_socket, output_audio)

            # 记录对话日志
            self.log_conversation(transcript, response_text)

        except socket.timeout:
            logger.warning(f"客户端 {addr} 操作超时")
        except Exception as e:
            logger.error(f"处理客户端 {addr} 时发生错误: {e}")
        finally:
            client_socket.close()
            logger.info(f"客户端 {addr} 连接已关闭")

    def receive_audio_data(self, client_socket):
        try:
            audio_data = bytearray()
            packet_count = 0
            
            while True:
                # 读取包头
                header = client_socket.recv(2)
                if len(header) != 2:
                    logger.warning("包头长度错误")
                    return None
                    
                packet_size = struct.unpack('>H', header)[0]
                if packet_size == 0xFFFF:
                    logger.debug("收到结束标志")
                    break
                    
                # 接收数据包
                chunk = bytearray()
                while len(chunk) < packet_size:
                    part = client_socket.recv(packet_size - len(chunk))
                    if not part:
                        raise ConnectionError("连接中断")
                    chunk.extend(part)
                    
                audio_data.extend(chunk)
                packet_count += 1
                logger.debug(f"收到第 {packet_count} 包，大小 {len(chunk)} 字节")

            # 验证数据长度
            if len(audio_data) < 16000 * 2:  # 至少1秒音频（16000样本*2字节）
                logger.error(f"音频数据过短: {len(audio_data)} 字节")
                return None
                
            return self._save_temp_audio(audio_data)
            
        except Exception as e:
            logger.error(f"数据接收失败: {str(e)}")
            return None

    def speech_to_text(self, audio_path):
        """语音识别"""
        try:
            start_time = datetime.datetime.now()
            transcript = self.asr_engine.transcribe(audio_path)
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            
            logger.info(f"语音识别成功 | 时长: {elapsed:.2f}s")
            logger.debug(f"识别结果: {transcript}")
            return transcript

        except Exception as e:
            logger.error(f"语音识别失败: {e}")
            return None

    def generate_response(self, prompt):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                start_time = datetime.datetime.now()
                response = ""
                
                # 添加超时参数
                for chunk in self.ollama_client.generate(
                    model="qwen2.5:latest",
                    prompt=prompt,
                    stream=True,
                    options={'timeout': 30}  # 添加30秒超时
                ):
                    if not chunk.get("response", ""):
                        raise ValueError("空响应")
                    response += chunk["response"]
                    
                return response
                
            except Exception as e:
                logger.warning(f"生成回复尝试 {attempt+1}/{max_retries} 失败: {e}")
                if attempt == max_retries - 1:
                    return "抱歉，我现在无法处理这个请求"
                time.sleep(2)

    def text_to_speech(self, text):
        """语音合成"""
        try:
            start_time = datetime.datetime.now()
            output_path = self.tts_engine.generate_audio(text)
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            
            logger.info(f"语音合成成功 | 时长: {elapsed:.2f}s")
            logger.debug(f"音频文件: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"语音合成失败: {e}")
            return None

    def send_audio_data(self, client_socket, audio_path):
        """改进后的音频发送"""
        try:
            with open(audio_path, "rb") as f:
                while True:
                    data = f.read(2048)  # 减小数据块大小
                    if not data:
                        break
                    # 添加包头
                    header = struct.pack('>H', len(data))
                    client_socket.sendall(header + data)
            # 发送结束标志
            end_flag = struct.pack('>H', 0xFFFF)
            client_socket.sendall(end_flag)
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    def log_conversation(self, input_text, output_text):
        """记录对话日志"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = f"./dialogue_history/{timestamp}.txt"
            
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"[{timestamp}] 用户输入:\n{input_text}\n\n")
                f.write(f"[{timestamp}] 系统回复:\n{output_text}\n")
            
            logger.info(f"对话记录已保存至 {log_file}")

        except Exception as e:
            logger.error(f"记录对话日志失败: {e}")
    
    def _save_temp_audio(self, raw_data: bytes) -> str:
        """将原始音频数据保存为WAV文件"""
        temp_dir = "./temp_audio"
        os.makedirs(temp_dir, exist_ok=True)
        
        filename = f"recv_{self.temp_audio_counter:04d}.wav"
        self.temp_audio_counter += 1
        filepath = os.path.join(temp_dir, filename)
        
        try:
            with wave.open(filepath, 'wb') as wav_file:
                wav_file.setnchannels(1)          # 单声道
                wav_file.setsampwidth(2)          # 16bit=2字节
                wav_file.setframerate(16000)      # 采样率
                wav_file.writeframes(raw_data)    # 写入原始数据
            return filepath
        except Exception as e:
            logger.error(f"保存临时音频失败: {str(e)}")
            return None
            
    def handle_client(self, client_socket, addr):
        """处理持续对话"""
        try:
            while True:
                # 1. 接收语音
                input_audio = self.receive_audio_data(client_socket)
                if not input_audio:
                    logger.warning("音频接收中断")
                    break
                
                # 2. 语音识别
                transcript = self.speech_to_text(input_audio)
                if not transcript:
                    client_socket.send(b"<ERROR>")
                    continue
                
                # 3. 生成回复
                response_text = self.generate_response(transcript)
                if not response_text:
                    client_socket.send(b"<ERROR>")
                    continue
                
                # 4. 语音合成
                output_audio = self.text_to_speech(response_text)
                if not output_audio:
                    client_socket.send(b"<ERROR>")
                    continue
                
                # 5. 发送回复
                self.send_audio_data(client_socket, output_audio)
                
        except ConnectionResetError:
            logger.warning(f"客户端 {addr} 主动断开")
        finally:
            client_socket.close()
            
if __name__ == "__main__":
    server = AeshtronServer()
    server.start_server()
    self.temp_audio_counter = 0
