import datetime
import os
import socket
import threading
import time
import wave
import ollama
import struct
import numpy as np

from loguru import logger
# 假设 VoiceRecognition 和 TTSEngine 类已经正确定义
from ASR.ASR import VoiceRecognition
from TTS.pyttxs3_TTS import TTSEngine


class AeshtronServer:
    def __init__(self):
        self.host = "0.0.0.0"
        self.port = 8080
        self.sample_rate = 16000
        self.channels = 1
        self.sample_width = 2  # 16bit = 2 bytes
        self.client_timeout = 300  # 客户端超时时间（秒）
        self.temp_audio_counter = 0  # 初始化临时音频计数器

        # 定义状态常量
        self.STATE_IDLE = "STATE_IDLE"
        self.STATE_RECORDING = "STATE_RECORDING"
        self.STATE_PLAYING = "STATE_PLAYING"
        self.SystemState = self.STATE_IDLE

        self.MAX_PACKET_SIZE = 4096  # 新增常量
        self.END_FLAG = 0xFFFF 

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
        client_id = f"{addr[0]}:{addr[1]}"
        self.asr_engine.create_stream(client_id)
        wake_word_detected = False  # 新增状态变量

        try:
            while True:
                if self.SystemState == self.STATE_IDLE:
                    chunk = self.receive_audio_chunk(client_socket)
                    if chunk is None:
                        break

                    # 流式处理
                    result = self.asr_engine.process_audio_chunk(client_id, chunk)

                    if result is True:  # 唤醒确认
                        wake_word_detected = True
                        client_socket.send(b"WAKE_CONFIRMED")
                        logger.info(f"检测到唤醒词，客户端{client_id}开始录音")
                        self.SystemState = self.STATE_RECORDING  # 修正赋值语句
                    else:
                        logger.debug("未检测到唤醒词")

                elif self.SystemState == self.STATE_RECORDING:
                    logger.info("开始接收音频数据")
                    try:
                        # 添加接收超时
                        client_socket.settimeout(30)  # 30秒接收超时
                        audio_path = self.receive_audio_data(client_socket)
                        if audio_path:
                            transcript = self.speech_to_text(audio_path)
                            logger.info("音频数据接收成功，开始语音识别")

                            if transcript:
                                logger.info("语音识别成功，开始生成回复")
                                response = self.generate_response(transcript)
                                logger.info("回复生成成功，开始语音合成")
                                output_audio = self.text_to_speech(response)
                                if output_audio:
                                    self.SystemState = self.STATE_PLAYING
                                    logger.info("语音合成成功，开始发送音频数据")
                                    if self.send_audio_data(client_socket, output_audio):
                                        client_socket.send(b"DATA_RECEIVED\n")
                                        logger.info(f"客户端{client_id}音频发送完成")
                        else:
                            logger.warning("收到空音频数据")
                            self.SystemState = self.STATE_IDLE
                    except socket.timeout:
                        logger.error("音频接收超时，返回空闲状态")
                        self.SystemState = self.STATE_IDLE
                    wake_word_detected = False  # 处理完后重置状态
                    self.SystemState = self.STATE_IDLE

                elif self.SystemState == self.STATE_PLAYING:
                    # 这里可以添加播放完成后的检查逻辑，如果有必要
                    # 目前简单认为播放完成后回到空闲状态
                    self.SystemState = self.STATE_IDLE

        except Exception as e:
            logger.error(f"客户端处理异常: {e}")
        finally:
            if client_id in self.asr_engine.active_streams:
                del self.asr_engine.active_streams[client_id]
            wake_word_detected = False  # 重置状态
            client_socket.close()

    def receive_audio_chunk(self, client_socket):
        try:
            # 设置包头接收超时
            client_socket.settimeout(5)
            header = self.recv_all(client_socket, 2)
            client_socket.settimeout(None)
            if len(header) != 2:
                return None
            packet_size = struct.unpack('>H', header)[0]
            if packet_size == 0xFFFF:
                return None
            data = client_socket.recv(packet_size)
            return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0
        except socket.timeout:
            logger.error("接收音频块包头超时")
            return None
        except Exception:
            return None

    def receive_audio_data(self, client_socket):
        """返回保存的音频文件路径，而不是生成器"""
        try:
            audio_data = bytearray()
            packet_count = 0
            
            # 使用缓存机制处理粘包
            buffer = bytearray()
            start_time = time.time()
            
            while True:
                # 读取包头
                while len(buffer) < 2:
                    packet = client_socket.recv(2 - len(buffer))
                    if not packet:
                        if time.time() - start_time > 30:
                            raise TimeoutError("接收超时")
                        continue
                    buffer.extend(packet)
                    start_time = time.time()
                
                # 解析包头（小端）
                chunk_size = struct.unpack('<H', buffer[:2])[0]
                buffer = buffer[2:]
                
                # 处理结束标志
                if chunk_size == 0xFFFF:
                    logger.debug("收到合法结束标志")
                    break
                    
                # 校验长度合法性
                if chunk_size == 0 or chunk_size > self.MAX_PACKET_SIZE:
                    logger.warning(f"非法包长度: {chunk_size}，丢弃数据")
                    buffer.clear()
                    continue
                    
                # 读取数据块
                while len(buffer) < chunk_size:
                    need = chunk_size - len(buffer)
                    packet = client_socket.recv(need)
                    if not packet:
                        raise ConnectionError("连接中断")
                    buffer.extend(packet)
                
                # 存储有效数据
                valid_data = buffer[:chunk_size]
                audio_data.extend(valid_data)
                buffer = buffer[chunk_size:]
                packet_count += 1
                logger.debug(f"收到第 {packet_count} 包，有效数据 {len(valid_data)} 字节")

            # 保存临时文件并返回路径
            if len(audio_data) >= 16000 * 2:  # 至少1秒音频
                return self._save_temp_audio(audio_data)
            else:
                logger.error(f"音频数据过短: {len(audio_data)} 字节")
                return None
                
        except Exception as e:
            logger.error(f"音频接收失败: {str(e)}")
            return None

    def recv_all(self, sock, n):
        """确保接收指定长度的数据"""
        data = bytearray()
        start_time = time.time()
        while len(data) < n:
            if time.time() - start_time > 5:  # 5 秒超时
                logger.error("接收指定长度数据超时")
                return None
            packet = sock.recv(n - len(data))
            if not packet:
                return None
            data.extend(packet)
        return data

    def speech_to_text(self, audio_path):
        """语音识别"""
        try:
            start_time = datetime.datetime.now()
            with wave.open(audio_path, 'rb') as wf:
                audio_data = wf.readframes(wf.getnframes())
                audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32767.0
            transcript = self.asr_engine.transcribe_np(audio_np)
            elapsed = (datetime.datetime.now() - start_time).total_seconds()

            logger.info(f"语音识别成功 | 时长: {elapsed:.2f}s")
            logger.debug(f"识别结果: {transcript}")
            return transcript

        except Exception as e:
            logger.error(f"语音识别失败: {e}")
            return None

    def generate_response(self, prompt):
        log_file_path = "./dialogue_history/chat_log.txt"  # 日志文件路径
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            response = ""

            # 记录用户输入到日志文件
            with open(log_file_path, "a", encoding="utf-8") as file:
                file.write(f"{current_time} - 用户: {prompt}\n")

            # 添加超时参数
            print("Aeshtron: ", end="", flush=True)
            for chunk in self.ollama_client.generate(
                model="qwen2.5:latest",
                prompt=prompt,
                stream=True,
                options={'timeout': 30}  # 添加30秒超时
            ):
                response_part = chunk["response"]
                response += response_part
                print(response_part, end="", flush=True)
            print()  # 换行

            # 记录模型回复到日志文件
            with open(log_file_path, "a", encoding="utf-8") as file:
                file.write(f"{current_time} - Aeshtron: {response}\n")
                file.write("-" * 50 + "\n")

            return response

        except Exception as e:
            logger.warning(f"生成回复尝试失败: {e}")
            return "抱歉，我现在无法处理这个请求"

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
        """优化后的音频发送"""
        try:
            with open(audio_path, "rb") as f:
                while True:
                    data = f.read(1024)  # 优化分块大小
                    if not data:
                        break

                    # 发送包头+数据
                    header = struct.pack('>H', len(data))
                    client_socket.sendall(header + data)

            # 发送结束标志
            end_flag = struct.pack('>H', 0xFFFF)
            client_socket.sendall(end_flag)
            return True
        except Exception as e:
            logger.error(f"音频发送失败: {e}")
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
                wav_file.setnchannels(1)  # 单声道
                wav_file.setsampwidth(2)  # 16bit=2字节
                wav_file.setframerate(16000)  # 采样率
                wav_file.writeframes(raw_data)  # 写入原始数据
            return filepath
        except Exception as e:
            logger.error(f"保存临时音频失败: {str(e)}")
            return None


if __name__ == "__main__":
    server = AeshtronServer()
    server.start_server()
