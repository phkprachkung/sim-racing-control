#include <Wire.h>

// ----- การตั้งค่าฮาร์ดแวร์ AS5600 -----
const int AS5600_ADDR = 0x36;
float centerAngle = 180.0; 

// ----- ตัวแปรระบบเซ็นเซอร์ -----
float currentContinuousAngle = 0.0;
float previousRawAngle       = -1.0;
float filteredAngle          = 0.0;
bool  isInitialized          = false;

// ============================================================
//  HELPER: อ่านองศาจาก AS5600
// ============================================================
void recoverI2C() {
  Wire.end();
  delayMicroseconds(100);
  Wire.begin();
  Wire.setClock(100000);
  Wire.setWireTimeout(5000, false);
  Wire.clearWireTimeoutFlag();
}

float readAS5600() {
  Wire.beginTransmission(AS5600_ADDR);
  // ✅ เปลี่ยนมาอ่าน Register 0x0E (ANGLE) แทน 0x0C (RAW ANGLE)
  // เพราะ 0x0E จะผ่านระบบกรองสัญญาณ (Filter & Hysteresis) ในชิปมาให้แล้ว สัญญาณจะนิ่งกว่ามาก!
  Wire.write(0x0E); 
  if (Wire.endTransmission() != 0) {
    if (Wire.getWireTimeoutFlag()) recoverI2C();
    return -1.0;
  }
  
  Wire.requestFrom(AS5600_ADDR, 2);
  if (Wire.available() == 2) {
    int highByte = Wire.read();
    int lowByte  = Wire.read();
    int rawValue = (highByte << 8) | lowByte;
    return (rawValue * 360.0) / 4096.0;
  }
  return -1.0;
}

// ============================================================
//  DIAGNOSTIC: ตรวจสอบความแรงแม่เหล็ก (Magnet Status)
// ============================================================
void printAS5600Status() {
  Wire.beginTransmission(AS5600_ADDR);
  Wire.write(0x0B); // Status Register
  if (Wire.endTransmission() != 0) {
    Serial.println("{\"Magnet_Error\": \"I2C failed to read status\"}");
    return;
  }
  
  Wire.requestFrom(AS5600_ADDR, 1);
  if (Wire.available() == 1) {
    byte status = Wire.read();
    bool md = status & 0x20; // Bit 5: Magnet Detected
    bool ml = status & 0x10; // Bit 4: Magnet too Low (Too Strong)
    bool mh = status & 0x08; // Bit 3: Magnet too High (Too Weak)
    
    Serial.print("{\"Magnet_MD\": ");
    Serial.print(md ? "true" : "false");
    Serial.print(", \"Magnet_ML_TooStrong\": ");
    Serial.print(ml ? "true" : "false");
    Serial.print(", \"Magnet_MH_TooWeak\": ");
    Serial.print(mh ? "true" : "false");
    Serial.println("}");
  }
}

// ============================================================
//  SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(100000);
  Wire.setWireTimeout(5000, false);
  
  // ✅ ตรวจสอบสถานะแม่เหล็กตอนเริ่มต้นและพิมพ์ออกทาง Serial
  delay(500); // รอให้เซ็นเซอร์บู้ตเสร็จ
  printAS5600Status();
}

// ============================================================
//  LOOP
// ============================================================
void loop() {
  static unsigned long lastSensorRead = 0;
  static bool hadI2CError = false;

  // ---------- 1. อ่านเซ็นเซอร์ AS5600 (จำกัด 1000Hz ป้องกันบอร์ดรวน) ----------
  if (micros() - lastSensorRead >= 1000) {
    lastSensorRead = micros();
    float angle = readAS5600();

    if (angle >= 0.0) {
      if (!isInitialized) {
        previousRawAngle       = angle;
        currentContinuousAngle = angle;
        filteredAngle          = angle;
        isInitialized          = true;
        Serial.println("{\"Info\": \"AS5600 Connected & Initialized\"}");
      } else {
        if (hadI2CError) {
          previousRawAngle = angle;
          hadI2CError = false;
          Serial.println("{\"Info\": \"AS5600 Recovered\"}");
        } else {
          float delta = angle - previousRawAngle;
          if (delta < -180.0) delta += 360.0;
          else if (delta > 180.0) delta -= 360.0;

          // ✅ ตัดทิ้งถ้ากระโดดเกิน 45 องศาต่อมิลลิวินาที (กันสัญญาณรบกวนขีดสุด)
          if (fabsf(delta) < 45.0) {
            currentContinuousAngle += delta;
          }
        }
        
        previousRawAngle = angle;
        // ✅ ปรับ Filter ให้นุ่มนวลขึ้น (0.1 / 0.9) ช่วยลดอาการแกว่ง
        filteredAngle = 0.10f * currentContinuousAngle + 0.90f * filteredAngle;
      }
    } else {
      if (!hadI2CError) { 
        hadI2CError = true;
        Serial.println("{\"Error\": \"AS5600 Disconnected\"}");
      }
    }
  }

  // ---------- 2. รับคำสั่งจาก Python ----------
  if (Serial.available() > 0) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd.equals("ALIGN")) {
      float a = readAS5600();
      if (a >= 0.0) {
        centerAngle = a;
        isInitialized = false; 
        Serial.println("{\"Info\": \"Center Aligned\"}");
      } else {
        Serial.println("{\"Error\": \"Cannot align, AS5600 missing\"}");
      }
    }
    else if (cmd.equals("STATUS")) {
      printAS5600Status();
    }
  }

  // ---------- 3. ส่งข้อมูลให้ Python (Game) ----------
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint >= 10) { // ส่งข้อมูลที่ 100Hz
    lastPrint = millis();
    
    if (isInitialized && !hadI2CError) {
      float steerGame = filteredAngle - centerAngle;
      Serial.print("{\"Steer\": ");
      Serial.print(steerGame, 2);
      Serial.println("}");
    }
  }
}
