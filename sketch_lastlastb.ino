#include "mp3tf16p.h"
MP3Player mp3(7, 5);

// HC-SR04 pins
const int trigPin = 11;
const int echoPin = 12;

// Phone hook switch and LED pins
const int phoneLiftPin = 2;
const int phoneLiftLedPin = 8;

// Record button and LED pins
const int recordButtonPin = 3;
const int extraLedPin = 9;

// Playback state
bool isPlaying = false;
unsigned long playbackStartTime = 0;
const unsigned long maxPlayTime = 30000;

// Phone return delay state
bool phonJustReturned = false;
unsigned long phoneReturnTime = 0;
const unsigned long phoneReturnDelay = 2000;
int lastPhoneLiftState = HIGH; // inverted: HIGH = on hook at boot

// Toggle recording state
bool recActive = false;
bool lastRecordButtonState = HIGH;

// Last known states for change detection
bool lastIsPlaying = false;
int lastPhoneState = HIGH;
bool lastRecActive = false;

void sendStatus(int phoneLiftState) {
  String msg = "MP3:";
  msg += isPlaying ? "PLAYING" : "STOPPED";
  msg += ",PHONE:";
  msg += phoneLiftState == LOW ? "LIFTED" : "ON_HOOK";  // inverted
  msg += ",RECORD:";
  msg += recActive ? "PRESSED" : "IDLE";
  Serial.println(msg);
}

void setup() {
  Serial.begin(9600);
  mp3.initialize();

  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  pinMode(phoneLiftPin, INPUT_PULLUP);
  pinMode(phoneLiftLedPin, OUTPUT);

  pinMode(recordButtonPin, INPUT_PULLUP);
  pinMode(extraLedPin, OUTPUT);

  // Read actual pin states at boot to prevent false transitions on first loop
  lastPhoneLiftState = digitalRead(phoneLiftPin);
  lastRecordButtonState = digitalRead(recordButtonPin);

  // Force idle state on startup
  recActive = false;
  digitalWrite(phoneLiftLedPin, LOW);
  digitalWrite(extraLedPin, LOW);

  Serial.println("READY");
}

void loop() {
  // Read phone hook switch (inverted: LOW = lifted, HIGH = on hook)
  int phoneLiftState = digitalRead(phoneLiftPin);

  // TEMP DEBUG — remove after confirmed
  static int lastDebugPhone = -1;
  static int lastDebugRec = -1;
  if (phoneLiftState != lastDebugPhone) {
    Serial.print("DEBUG phoneLiftState=");
    Serial.println(phoneLiftState);
    lastDebugPhone = phoneLiftState;
  }
  int recBtnRaw = digitalRead(recordButtonPin);
  if (recBtnRaw != lastDebugRec) {
    Serial.print("DEBUG recordButtonPin=");
    Serial.println(recBtnRaw);
    lastDebugRec = recBtnRaw;
  }
  // END TEMP DEBUG

  // Detect LOW -> HIGH transition (phone just returned to hook)
  if (lastPhoneLiftState == LOW && phoneLiftState == HIGH) {
    phonJustReturned = true;
    phoneReturnTime = millis();
    if (recActive) {
      recActive = false;
    }
    Serial.println("PHONE:RETURNED");
  }
  lastPhoneLiftState = phoneLiftState;

  // LED ON when phone lifted (LOW), OFF when on hook (HIGH)
  digitalWrite(phoneLiftLedPin, phoneLiftState == LOW ? HIGH : LOW);

  // --- Toggle record button (INPUT_PULLUP: LOW = pressed) ---
  bool currentRecordButtonState = digitalRead(recordButtonPin);

  if (lastRecordButtonState == HIGH && currentRecordButtonState == LOW) {
    delay(50); // debounce
    if (phoneLiftState == LOW) {  // inverted: LOW = lifted
      recActive = !recActive;
      sendStatus(phoneLiftState);
    }
  }
  lastRecordButtonState = currentRecordButtonState;

  // Record LED ON while recording active, OFF otherwise
  digitalWrite(extraLedPin, recActive ? HIGH : LOW);

  // Send status update if any state has changed
  if (isPlaying != lastIsPlaying || phoneLiftState != lastPhoneState || recActive != lastRecActive) {
    sendStatus(phoneLiftState);
    lastIsPlaying = isPlaying;
    lastPhoneState = phoneLiftState;
    lastRecActive = recActive;
  }

  // If phone is lifted (LOW), ensure MP3 is silent and block sensor
  if (phoneLiftState == LOW) {
    if (isPlaying) {
      mp3.player.volume(0);
      isPlaying = false;
      Serial.println("PHONE:LIFTED,MP3:STOPPED");
    }
    return;
  }

  // If phone just returned, wait 2 seconds before allowing sensor to trigger
  if (phonJustReturned) {
    if (millis() - phoneReturnTime < phoneReturnDelay) {
      return;
    } else {
      phonJustReturned = false;
      Serial.println("SENSOR:ACTIVE");
    }
  }

  // If currently playing, check conditions
  if (isPlaying) {
    if (mp3.playCompleted()) {
      isPlaying = false;
      Serial.println("MP3:FINISHED");
    }
    else if (millis() - playbackStartTime > maxPlayTime) {
      isPlaying = false;
      Serial.println("MP3:TIMEOUT");
    }
  }

  // Only trigger a new playback if nothing is playing right now
  if (!isPlaying) {
    digitalWrite(trigPin, LOW);
    delayMicroseconds(2);
    digitalWrite(trigPin, HIGH);
    delayMicroseconds(10);
    digitalWrite(trigPin, LOW);

    long duration = pulseIn(echoPin, HIGH);
    int distance = duration * 0.034 / 2;

    if (distance < 85 && distance > 0) {
      Serial.println("SENSOR:TRIGGERED");
      mp3.player.volume(30);
      mp3.playTrackNumber(1, 30, false);
      isPlaying = true;
      playbackStartTime = millis();
    }
  }

  delay(50);
}