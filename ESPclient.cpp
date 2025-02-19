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
bool isPlaying = false;

void setup() {
  Serial.begin(115200);
  
  // 初始化LED
  FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);
  setLedColor(CRGB::Red); // 启动时红色
  
  // 初始化I2S
  initI2S();
  
  // 连接WiFi
  connectWiFi();
  
  // 连接服务器
  connectServer();
}

static unsigned long lastActionTime = millis();

void loop() {
    static bool isRecording = true;
    
    if (!tcpClient.connected()) {
        reconnect();
        lastActionTime = millis();
    }

    if (millis() - lastActionTime > 30000) { // 30秒超时
        Serial.println("系统超时，重置状态");
        reconnect();
        lastActionTime = millis();
        return;
    }

    if (isRecording) {
        recordAndSend();
        isRecording = false;
        setLedColor(CRGB::Blue); // 进入接收状态
    } else {
        if (receiveAndPlay()) {   // 如果成功播放完成
            isRecording = true;
            setLedColor(CRGB::Green); // 回到录音状态
        }
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

// 录音并发送
void recordAndSend() {
  const size_t chunkSize = 1024;
  int16_t buffer[chunkSize];
  size_t totalSent = 0;
  
  Serial.println("开始录音...");
  unsigned long start = millis();
  
  while(millis() - start < RECORD_TIME * 1000) {
    size_t bytesRead;
    i2s_read(I2S_NUM_0, (char*)buffer, sizeof(buffer), &bytesRead, portMAX_DELAY);
    
    if(bytesRead > 0) {
      // 发送数据包
      uint16_t packetSize = bytesRead;
      
      // 发送包头
      if(tcpClient.write((uint8_t*)&packetSize, sizeof(packetSize)) != sizeof(packetSize)) {
        Serial.println("包头发送失败");
        break;
      }
      
      // 发送音频数据
      size_t sent = tcpClient.write((uint8_t*)buffer, bytesRead);
      if(sent != bytesRead) {
        Serial.println("数据发送不完整");
        break;
      }
      
      totalSent += sent;
      Serial.printf("已发送 %d 字节\n", totalSent);
    }
  }
  
  // 发送结束标志
  uint16_t endFlag = 0xFFFF;
  tcpClient.write((uint8_t*)&endFlag, sizeof(endFlag));
  Serial.printf("总计发送 %d 字节\n", totalSent);
  
  // 等待确认
  unsigned long ackWaitStart = millis();
  while(tcpClient.available() < 2 && millis() - ackWaitStart < 3000) {
    delay(10);
  }
  
  if(tcpClient.available() >= 2) {
    uint16_t ack;
    tcpClient.readBytes((uint8_t*)&ack, 2);
    if(ack == 0xAAAA) {
      Serial.println("收到服务端确认");
    }
  }
}

// 接收并播放音频
bool receiveAndPlay() {
    const size_t headerSize = sizeof(uint16_t);
    uint16_t expectedSize = 0;
    bool playbackComplete = false;
    
    while(tcpClient.available() >= headerSize) {
        tcpClient.readBytes((uint8_t*)&expectedSize, headerSize);
        
        if(expectedSize == 0xFFFF) { 
            playbackComplete = true;
            break;
        }
        
        int16_t audioBuffer[expectedSize];
        size_t received = tcpClient.readBytes((uint8_t*)audioBuffer, expectedSize);
        
        if(received == expectedSize) {
            size_t written;
            i2s_write(I2S_NUM_1, (const char*)audioBuffer, received, &written, portMAX_DELAY);
        }
    }
    return playbackComplete;
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
  connectWiFi();
  connectServer();
}

// LED状态指示
void handleLED() {
  static uint8_t hue = 0;
  if(isPlaying){
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
