library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.STD_LOGIC_ARITH.ALL;
use IEEE.STD_LOGIC_UNSIGNED.ALL;

-- PicoX8 implements the interface between the PX-80 expansion bus
-- interface and the RP2040.  The PX-80 reads and writes the registers
-- in the CPLD using the Z80 I/O bus protocol.  The RP2040 accesses the
-- registers using an 8 bit wide, bidirectional interface with a 3 bit address
-- bus, a direction signal ('0' => read, '1' => write) and a strobe signal.
-- Transfers from and to the Pico need to be synchronized to the Z80 clock
-- signal, with the RP2040 making changes only near the rising edge and
-- sampling near the falling edge.

entity PicoX8 is
  Port (
    -- PX-8 expansion bus
    clk                 : in    std_logic;
    ioreq_n             : in    std_logic;
    rd_n                : in    std_logic;
    wr_n                : in    std_logic;
    reset_n             : in    std_logic;
    address             : in    std_logic_vector(7 downto 0);
    data                : inout std_logic_vector(7 downto 0);
    -- Serial port receiver (needs inverter)
    rx_n                : out   std_logic;
    rx                  : in    std_logic;

    -- Pico interface
    -- IRQ signals
    irq_tone_dialer     : out   std_logic;
    irq_modem_control   : out   std_logic;
    irq_ramdisk_command : out   std_logic;
    irq_ramdisk_obf     : out   std_logic;
    -- Pico register access port
    pico_data           : inout std_logic_vector(7 downto 0);
    pico_addr           : in    std_logic_vector(2 downto 0);
    pico_dir            : in    std_logic;
    pico_stb            : in    std_logic;

    -- LEDs for debugging
    led0                : out   std_logic;
    led1                : out   std_logic;
    led2                : out   std_logic;
    led3                : out   std_logic
    );
end PicoX8;

architecture Behavioral of PicoX8 is
  constant PX8_TONE_DIALER      : std_logic_vector(7 downto 0) := x"84";
  constant PX8_MODEM_CONTROL    : std_logic_vector(7 downto 0) := x"85";
  constant PX8_MODEM_STATUS     : std_logic_vector(7 downto 0) := x"86";
  constant PX8_RAMDISK_DATA     : std_logic_vector(7 downto 0) := x"80";
  constant PX8_RAMDISK_CONTROL  : std_logic_vector(7 downto 0) := x"81";
  constant PICO_TONE_DIALER     : std_logic_vector(2 downto 0) := "000";
  constant PICO_MODEM_CONTROL   : std_logic_vector(2 downto 0) := "001";
  constant PICO_MODEM_STATUS    : std_logic_vector(2 downto 0) := "010";
  constant PICO_RAMDISK_DATA    : std_logic_vector(2 downto 0) := "011";
  constant PICO_RAMDISK_CONTROL : std_logic_vector(2 downto 0) := "100";
  signal modem_tone_dialer      : std_logic_vector(7 downto 0);
  signal modem_control          : std_logic_vector(7 downto 0);
  signal modem_status           : std_logic_vector(7 downto 0);
  signal ramdisk_data           : std_logic_vector(7 downto 0);
  signal ramdisk_command        : std_logic_vector(7 downto 0);
  signal ramdisk_ibf            : std_logic;
  signal ramdisk_obf            : std_logic;
  signal data_out               : std_logic_vector(7 downto 0);
  signal oe                     : std_logic;
  signal pico_data_out          : std_logic_vector(7 downto 0);
  signal pico_oe                : std_logic;
begin
  process(clk)
  begin
    if falling_edge(clk) then
      if (reset_n = '0') then
        modem_tone_dialer   <= x"00";
        modem_control       <= x"00";
        modem_status        <= x"00";
        ramdisk_data        <= x"00";
        ramdisk_command     <= x"00";
        ramdisk_ibf         <= '0';
        ramdisk_obf         <= '0';
        irq_tone_dialer     <= '0';
        irq_modem_control   <= '0';
        irq_ramdisk_command <= '0';
      else
        led1                <= '1';
        led2                <= '1';
        led3                <= '1';

        -- Handle access from the Z80 side
        if (ioreq_n = '0') then
          oe <= '0';
          if (rd_n = '0') then
            oe <= '1';
            case address is
              when PX8_MODEM_STATUS =>
                led1 <= '0';
                data_out <= modem_status;
              when PX8_RAMDISK_DATA =>
                data_out <= ramdisk_data;
                ramdisk_ibf <= '0';
              when PX8_RAMDISK_CONTROL =>
                led2 <= '0';
                data_out <= (0 => ramdisk_ibf, 1 => ramdisk_obf, others => '0');
              when others =>
                data_out <= x"00";
            end case;
          elsif (wr_n = '0') then
            case address is
              when PX8_TONE_DIALER =>
                modem_tone_dialer <= data;
                irq_tone_dialer <= '1';
              when PX8_MODEM_CONTROL =>
                modem_control <= data;
                irq_modem_control <= '1';
              when PX8_RAMDISK_DATA =>
                ramdisk_data <= data;
                ramdisk_obf <= '1';
              when PX8_RAMDISK_CONTROL =>
                ramdisk_command <= data;
                irq_ramdisk_command <= '1';
              when others =>
                null;
            end case;
          end if;
        end if;
        -- Handle access from the Pico side
        if (pico_stb = '1') then
          pico_oe <= '0';

          if (pico_dir = '0') then
            pico_oe <= '1';
            case pico_addr is
              when PICO_TONE_DIALER =>
                pico_data_out <= modem_tone_dialer;
                irq_tone_dialer <= '0';
              when PICO_MODEM_CONTROL =>
                pico_data_out <= modem_control;
                irq_modem_control <= '0';
              when PICO_RAMDISK_DATA =>
                pico_data_out <= ramdisk_data;
                ramdisk_obf <= '0';
              when PICO_RAMDISK_CONTROL =>
                pico_data_out <= ramdisk_command;
                irq_ramdisk_command <= '0';
                ramdisk_ibf <= '0';             -- flush buffers
                ramdisk_obf <= '0';
              when others =>
                pico_data_out <= x"00";
            end case;
          else
            case pico_addr is
              when PICO_MODEM_STATUS =>
                led3 <= '0';
                modem_status <= pico_data;
              when PICO_RAMDISK_DATA =>
                ramdisk_data <= pico_data;
                ramdisk_ibf <= '1';
              when others =>
                null;
            end case;
          end if;
        end if;
      end if;
    end if;
  end process;

  data <= data_out when oe = '1' else (others => 'Z');
  pico_data <= pico_data_out when pico_oe = '1' else (others => 'Z');

  -- RS232 RX on expansion bus needs to be inverted
  rx_n <= not rx;

  -- RAM-Disk handshake signals
  irq_ramdisk_obf <= ramdisk_obf;

  led0 <= '0';

end Behavioral;

