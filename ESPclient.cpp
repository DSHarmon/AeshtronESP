#include <WiFi.h>
#include <FastLED.h>
#include <driver/i2s.h>

// 硬件配置
#define LED_PIN      48     // WS2812数据引脚
#define NUM_LEDS     1      // LED数量
CRGB leds[NUM_LEDS];

// 网络配置
const char* WIFI_SSID = "DSHarmonMate60pro";
const char* WIFI_PASSWORD = "3zwhjha9fhhm4h";
const char* SERVER_IP = "192.168.43.84"; // 电脑IP
const int SERVER_PORT = 8080;            // 与Python端统一端口
const int CONNECT_TIMEOUT = 15000;    // 连接超时15秒

// I2S配置
#define SAMPLE_RATE 16000
#define BUFFER_SIZE (SAMPLE_RATE * 3) // 减少录音时间到3秒
#define RECORD_TIME 3        // 录音时长(秒)
// #define BUFFER_SIZE (SAMPLE_RATE * RECORD_TIME) // 音频缓冲区

// 引脚定义
const i2s_pin_config_t i2sInPins = { // INMP441麦克风
  .bck_io_num = 4,  // BCK -> GPIO4
  .ws_io_num = 5,   // WS  -> GPIO5
  .data_out_num = -1,
  .data_in_num = 6   // SD  -> GPIO6
};

const i2s_pin_config_t i2sOutPins = { // MAX98357功放
  .bck_io_num = 15, // BCK -> GPIO15
  .ws_io_num = 16,  // WS  -> GPIO16
  .data_out_num = 7, // DIN -> GPIO7
  .data_in_num = -1
};

WiFiClient tcpClient;
// bool isPlaying = false;

enum SystemState {
  STATE_IDLE,        // 空闲等待唤醒
  STATE_RECORDING,   // 正在录音
  STATE_PLAYING      // 正在播放
};
SystemState currentState = STATE_IDLE;

unsigned long silenceStart = 0;
const int SILENCE_TIMEOUT = 500; // 1.5 秒静音停止

unsigned long lastDataTransferTime = 0;
bool dataTransferred = false;

void setup() {
  Serial.begin(115200);

  FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);
  setLedColor(CRGB::Red);

  initI2S();
  connectWiFi();
  connectServer();

  lastDataTransferTime = millis();
}

void loop() {

  if (!tcpClient.connected()) {
    reconnect();
    return;
  }

  switch(currentState){
    case STATE_IDLE:
      checkWakeWord();
      break;
      
    case STATE_RECORDING:
      recordAndSend();
      break;
      
    case STATE_PLAYING:
      receiveAndPlay();
 // 播放完成后回到空闲
      break;
  }
  
  handleLED();
}


// 初始化I2S子系统
void initI2S() {
  // 输入配置
  i2s_config_t i2sInConfig = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 1024
  };
  i2s_driver_install(I2S_NUM_0, &i2sInConfig, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &i2sInPins);

  // 输出配置
  i2s_config_t i2sOutConfig = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 1024
  };
  i2s_driver_install(I2S_NUM_1, &i2sOutConfig, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &i2sOutPins);
}

// 合并后的录音和发送函数
// 合并后的录音和发送函数
void recordAndSend() {
  const size_t chunkSize = 1024;
  int16_t buffer[chunkSize];
  unsigned long startTime = millis();

  while((millis() - startTime) < RECORD_TIME*1000){
    size_t bytesRead;
    if(i2s_read(I2S_NUM_0, buffer, sizeof(buffer), &bytesRead, 0) == ESP_OK){
      sendAudioChunk(buffer, bytesRead);
      // VAD检测
      if(isSilence(buffer, bytesRead/2)){
        if(silenceStart == 0) silenceStart = millis();
        else if(millis()-silenceStart > SILENCE_TIMEOUT) break;
      } else {
        silenceStart = 0;
      }
    }
  }
  // 发送结束标志
  uint16_t endFlag = 0xFFFF;
  size_t sent = tcpClient.write((uint8_t*)&endFlag, sizeof(endFlag));
  Serial.printf("已发送结束标志，发送字节数：%d\n", sent);
  if (sent != sizeof(endFlag)) {
      Serial.println("发送结束标志失败");
      // 可以添加重试逻辑
  } else {
     // 等待服务器确认消息
      unsigned long confirmStartTime = millis();
      while (millis() - confirmStartTime < 30000) { // 等待 5 秒
          if (tcpClient.available() > 0) {
              String response = tcpClient.readStringUntil('\n');
              if (response == "DATA_RECEIVED") {
                  currentState = STATE_PLAYING;
                  Serial.println("开始接收回复");
                  break;
              }
          }
      }
      if (currentState != STATE_PLAYING) {
          Serial.println("未收到服务器确认消息，保持当前状态");
      }
  }
}

// 接收并播放音频
void receiveAndPlay() {
  const size_t headerSize = 2;
  uint16_t packetSize = 0;
  int16_t audioBuffer[2048];

  while (true) {
    // 等待包头
    while (tcpClient.available() < headerSize) {
      delay(1);
      if (!tcpClient.connected()) {
        Serial.println("网络连接断开，停止播放");
        return;
      }
    }

    // 读取包头
    tcpClient.readBytes((uint8_t*)&packetSize, headerSize);

    // 结束标志
    if (packetSize == 0xFFFF) {
      break;
    }

    // 读取音频数据
    size_t received = 0;
    while (received < packetSize) {
      if (!tcpClient.connected()) {
        Serial.println("网络连接断开，停止播放");
        return;
      }
      size_t toRead = min(sizeof(audioBuffer), packetSize - received);
      size_t actualRead = tcpClient.readBytes((uint8_t*)audioBuffer, toRead);
      if (actualRead == 0) {
        // 没有读取到数据，可能网络有问题，等待一段时间后重试
        delay(10);
        continue;
      }

      // 实时播放
      size_t written;
      i2s_write(I2S_NUM_1, audioBuffer, actualRead, &written, portMAX_DELAY);
      received += actualRead;
    }
  }

  Serial.println("播放完成");

  // 停止 I2S 输出，清空 DMA 缓冲区并等待传输完成
  i2s_zero_dma_buffer(I2S_NUM_1);
  vTaskDelay(pdMS_TO_TICKS(100));

  // 卸载 I2S 驱动
  i2s_driver_uninstall(I2S_NUM_1);

  // 重新初始化 I2S 输出配置
  i2s_config_t i2sOutConfig = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 1024
  };
  i2s_driver_install(I2S_NUM_1, &i2sOutConfig, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &i2sOutPins);

  currentState = STATE_IDLE;
}
// 网络连接管理
void connectWiFi() {
  if(WiFi.status() == WL_CONNECTED) return;
  
  Serial.printf("连接WiFi: %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  while(WiFi.status() != WL_CONNECTED){
    Serial.print(".");
    setLedColor(CRGB::Red);
    delay(500);
    setLedColor(CRGB::Black);
    delay(500);
  }
  
  Serial.printf("\n已连接，IP地址: %s\n", WiFi.localIP().toString().c_str());
}

void connectServer() {
  Serial.printf("连接服务器 %s:%d...\n", SERVER_IP, SERVER_PORT);
  
  int retryCount = 0;
  while(!tcpClient.connect(SERVER_IP, SERVER_PORT, CONNECT_TIMEOUT)) { // 添加超时参数
    // Serial.printf("连接失败，错误代码: %d\n", tcpClient.status());
    Serial.printf("连接失败");
    if(++retryCount > 3){
      Serial.println("重置网络堆栈...");
      WiFi.disconnect(true);
      delay(1000);
      WiFi.reconnect();
      retryCount = 0;
    }
    
    // 指数退避算法
    delay((1 << retryCount) * 1000); // 2,4,8秒间隔
    setLedColor(CRGB::Orange);
  }
  
  // 配置TCP参数
  tcpClient.setNoDelay(true);       // 禁用Nagle算法
  tcpClient.setTimeout(5000);       // 设置IO超时
  Serial.println("服务器连接成功！");
}


void reconnect() {
  setLedColor(CRGB::Purple);
  Serial.println("连接丢失，尝试重新连接...");
  
  tcpClient.stop();
  WiFi.disconnect();
  delay(1000);
  connectWiFi();
  connectServer();
  currentState = STATE_IDLE; // 重置状态
}

// LED状态指示
void handleLED() {
  static uint8_t hue = 0;
  if(currentState == STATE_PLAYING){
    // 播放时蓝色呼吸灯
    leds[0] = CHSV(hue++, 100, beatsin8(20, 50, 255));
  } else {
    // 空闲时绿色呼吸灯
    leds[0] = CHSV(64, 100, beatsin8(10, 50, 200));
  }
  FastLED.show();
  FastLED.delay(20);
}

void setLedColor(CRGB color) {
  fill_solid(leds, NUM_LEDS, color);
  FastLED.show();
}

void checkWakeWord() {
  static int16_t buffer[512];
  size_t bytesRead;
  
  if(i2s_read(I2S_NUM_0, buffer, sizeof(buffer), &bytesRead, 0) == ESP_OK){
    sendAudioChunk(buffer, bytesRead);
    // 检查服务器响应
    if(tcpClient.available() > 0){
      String response = tcpClient.readStringUntil('\n');
      if(response == "WAKE_CONFIRMED"){
        currentState = STATE_RECORDING;
        silenceStart = 0;
        Serial.println("进入录音状态");
      }
    }
  }
}

bool isSilence(int16_t* samples, size_t count) {
  const int16_t SILENCE_THRESHOLD = 500; // 静音阈值
  for (int i=0; i<count; i++) {
    if (abs(samples[i]) > SILENCE_THRESHOLD) {
      return false;
    }
  }
  return true;
}

void sendAudioChunk(void* data, size_t bytes) {
    const size_t MAX_PACKET_SIZE = 4096; // 限制最大数据包大小为 4096 字节
    if (bytes > MAX_PACKET_SIZE) {
        // 分割数据并分多次发送
        size_t offset = 0;
        while (offset < bytes) {
            size_t chunkSize = std::min(MAX_PACKET_SIZE, bytes - offset);
            uint16_t header = chunkSize;
            tcpClient.write((uint8_t*)&header, sizeof(header));
            tcpClient.write((uint8_t*)((uint8_t*)data + offset), chunkSize);
            offset += chunkSize;
        }
    } else {
        uint16_t header = bytes;
        tcpClient.write((uint8_t*)&header, sizeof(header));
        tcpClient.write((uint8_t*)data, bytes);
    }
    delay(10);
}
