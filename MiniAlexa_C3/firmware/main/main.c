// Mini-Alexa C3: graba voz (GPIO3) y reproduce la respuesta por PDM (GPIO10).
// Base: firmware de la Parte 1 (misma captura y misma cabecera C3AUDIO1).
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_adc/adc_continuous.h"
#include "driver/usb_serial_jtag.h"
#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include "esp_timer.h"
#include "soc/soc_caps.h"

#if !CONFIG_IDF_TARGET_ESP32C3
#error "Seleccionar esp32c3"
#endif

#define RATE 16000
#define SAMPLES (RATE * 6)        // 6 s de grabacion
#define PLAY_RATE 16000
#define CHUNK 2048                 // bytes por bloque de reproduccion (1024 muestras)
#define PIN_LED GPIO_NUM_8         // LED azul, activo en bajo
#define PIN_BTN GPIO_NUM_9         // boton BOOT, activo en bajo
#define PIN_PDM_OUT GPIO_NUM_10    // salida de audio hacia el filtro RC
#define PIN_PDM_CLK GPIO_NUM_6     // reloj PDM: no se conecta

static volatile bool overflow;
static uint8_t frame[1024];
static adc_continuous_data_t parsed[1024 / SOC_ADC_DIGI_RESULT_BYTES];
static uint8_t chunk[CHUNK];
// Muestras de 12 bits empaquetadas (2 muestras = 3 bytes) en bloques de 1 s,
// porque la RAM libre de la C3 esta fragmentada y no hay 192 KB seguidos.
#define PACKED_BYTES(n) ((n) / 2 * 3)
#define BLOCK_BYTES PACKED_BYTES(RATE)              // 24000 bytes = 1 s
#define NBLOCKS ((PACKED_BYTES(SAMPLES) + BLOCK_BYTES - 1) / BLOCK_BYTES)
static uint8_t *blocks[NBLOCKS];
static bool audio;                                   // true si se reservaron todos los bloques
static adc_continuous_handle_t adc;
static i2s_chan_handle_t spk;

static bool on_overflow(adc_continuous_handle_t h,const adc_continuous_evt_data_t *e,void *a)
{ (void)h;(void)e;(void)a;overflow=true;return false; }
static inline void store12(uint32_t i,uint16_t v)
{
    uint32_t off=(i>>1)*3;
    uint8_t *p=blocks[off/BLOCK_BYTES]+off%BLOCK_BYTES;
    if(i&1){p[1]|=(v&0x0F)<<4;p[2]=v>>4;}
    else{p[0]=v&0xFF;p[1]=(v>>8)&0x0F;}
}
static void put32(uint8_t *p,uint32_t n){for(int i=0;i<4;i++)p[i]=n>>(8*i);}
static uint32_t crc32_update(uint32_t crc,const uint8_t *p,size_t n)
{
    while(n--){crc^=*p++;for(int i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0);}
    return crc;
}
static bool send_all(const void *buffer,size_t len)
{
    const uint8_t *p=buffer;
    while(len){
        size_t c=len>1024?1024:len;
        int n=usb_serial_jtag_write_bytes(p,c,pdMS_TO_TICKS(2000));
        if(n<=0)return false;
        p+=n;len-=n;
    }
    return true;
}
static bool read_exact(uint8_t *p,size_t n,int timeout_ms)
{
    int64_t deadline=esp_timer_get_time()+(int64_t)timeout_ms*1000;
    while(n){
        int r=usb_serial_jtag_read_bytes(p,n,pdMS_TO_TICKS(20));
        if(r>0){p+=r;n-=r;}
        else if(esp_timer_get_time()>deadline)return false;
    }
    return true;
}

// 6 s a 16 kHz (12 bits empaquetados) y envio con cabecera + CRC32
static void record_and_send(void)
{
    uint32_t count=0,flags=audio?0:1;
    const uint8_t start='S';                       // aviso al PC: empieza la grabacion
    usb_serial_jtag_write_bytes(&start,1,pdMS_TO_TICKS(50));
    gpio_set_level(PIN_LED,0);
    if(audio){
        overflow=false;
        ESP_ERROR_CHECK(adc_continuous_flush_pool(adc));
        if(adc_continuous_start(adc)!=ESP_OK)flags|=2;
        else{
            int64_t deadline=esp_timer_get_time()+15000000;
            while(count<SAMPLES){
                uint32_t bytes=0,n=0;
                esp_err_t ret=adc_continuous_read(adc,frame,sizeof(frame),&bytes,1000);
                if(ret!=ESP_OK||esp_timer_get_time()>deadline){flags|=4;break;}
                if(adc_continuous_parse_data(adc,frame,bytes,parsed,&n)!=ESP_OK){flags|=8;break;}
                for(unsigned i=0;i<n&&count<SAMPLES;i++){
                    if(parsed[i].valid&&parsed[i].unit==ADC_UNIT_1&&parsed[i].channel==ADC_CHANNEL_3)store12(count++,(uint16_t)parsed[i].raw_data);
                    else flags|=16;
                }
                if(overflow){flags|=32;break;}
            }
            ESP_ERROR_CHECK(adc_continuous_stop(adc));
            if(overflow)flags|=32;
        }
    }
    gpio_set_level(PIN_LED,1);
    if(flags)count=0;
    count&=~1u;                                   // numero par de muestras
    uint8_t header[24];memcpy(header,"C3AUDIO1",8);
    uint32_t total=PACKED_BYTES(count),crc=0xffffffff;
    for(uint32_t off=0;off<total;off+=BLOCK_BYTES)
        crc=crc32_update(crc,blocks[off/BLOCK_BYTES],total-off<BLOCK_BYTES?total-off:BLOCK_BYTES);
    put32(header+8,RATE);put32(header+12,count);put32(header+16,~crc);put32(header+20,flags);
    if(!send_all(header,sizeof(header)))return;
    for(uint32_t off=0;off<total;off+=BLOCK_BYTES)
        if(!send_all(blocks[off/BLOCK_BYTES],total-off<BLOCK_BYTES?total-off:BLOCK_BYTES))return;
}

// Orden 'P' + longitud (4 bytes) + PCM int16 a 16 kHz.
// Por cada bloque leido se responde 'K' (control de flujo); al terminar, 'D'.
static void play_stream(void)
{
    uint8_t h[4];
    if(!read_exact(h,4,1000))return;
    uint32_t total=h[0]|(h[1]<<8)|(h[2]<<16)|((uint32_t)h[3]<<24);
    const uint8_t ok='K',done='D';
    size_t w;
    ESP_ERROR_CHECK(i2s_channel_enable(spk));
    while(total){
        size_t n=total>CHUNK?CHUNK:total;
        if(!read_exact(chunk,n,3000))break;     // el PC dejo de enviar
        total-=n;
        usb_serial_jtag_write_bytes(&ok,1,pdMS_TO_TICKS(100));
        i2s_channel_write(spk,chunk,n&~1u,&w,pdMS_TO_TICKS(1000));
    }
    memset(chunk,0,CHUNK);                       // silencio para vaciar el DMA
    for(int i=0;i<4;i++)i2s_channel_write(spk,chunk,CHUNK,&w,pdMS_TO_TICKS(500));
    ESP_ERROR_CHECK(i2s_channel_disable(spk));
    usb_serial_jtag_write_bytes(&done,1,pdMS_TO_TICKS(100));
}

static void speaker_init(void)
{
    i2s_chan_config_t cc=I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0,I2S_ROLE_MASTER);
    cc.auto_clear=true;
    ESP_ERROR_CHECK(i2s_new_channel(&cc,&spk,NULL));
    i2s_pdm_tx_config_t pc={
        .clk_cfg=I2S_PDM_TX_CLK_DAC_DEFAULT_CONFIG(PLAY_RATE),
        .slot_cfg=I2S_PDM_TX_SLOT_DAC_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_MONO),
        .gpio_cfg={.clk=PIN_PDM_CLK,.dout=PIN_PDM_OUT,.invert_flags={.clk_inv=false}},
    };
    ESP_ERROR_CHECK(i2s_channel_init_pdm_tx_mode(spk,&pc));
}

void app_main(void)
{
    gpio_reset_pin(PIN_LED);gpio_set_direction(PIN_LED,GPIO_MODE_OUTPUT);gpio_set_level(PIN_LED,1);
    gpio_reset_pin(PIN_BTN);gpio_set_direction(PIN_BTN,GPIO_MODE_INPUT);gpio_set_pull_mode(PIN_BTN,GPIO_PULLUP_ONLY);

    usb_serial_jtag_driver_config_t usb={.tx_buffer_size=4096,.rx_buffer_size=8192};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    audio=true;
    for(int b=0;b<NBLOCKS;b++)if(!(blocks[b]=malloc(BLOCK_BYTES)))audio=false;

    adc_continuous_handle_cfg_t cfg={.max_store_buf_size=16384,.conv_frame_size=sizeof(frame)};
    ESP_ERROR_CHECK(adc_continuous_new_handle(&cfg,&adc));
    adc_digi_pattern_config_t pattern={.atten=ADC_ATTEN_DB_12,.channel=ADC_CHANNEL_3,.unit=ADC_UNIT_1,.bit_width=SOC_ADC_DIGI_MAX_BITWIDTH};
    adc_continuous_config_t config={.sample_freq_hz=RATE,.conv_mode=ADC_CONV_SINGLE_UNIT_1,.pattern_num=1,.adc_pattern=&pattern};
    ESP_ERROR_CHECK(adc_continuous_config(adc,&config));
    adc_continuous_evt_cbs_t callbacks={.on_pool_ovf=on_overflow};
    ESP_ERROR_CHECK(adc_continuous_register_event_callbacks(adc,&callbacks,NULL));
    speaker_init();

    while(1){
        uint8_t command;
        if(usb_serial_jtag_read_bytes(&command,1,pdMS_TO_TICKS(20))==1){
            if(command=='R')record_and_send();
            else if(command=='P')play_stream();
            continue;
        }
        if(gpio_get_level(PIN_BTN)==0){               // pulsar BOOT = hablar
            vTaskDelay(pdMS_TO_TICKS(30));
            if(gpio_get_level(PIN_BTN)==0){
                record_and_send();
                while(gpio_get_level(PIN_BTN)==0)vTaskDelay(pdMS_TO_TICKS(10));
            }
        }
    }
}
