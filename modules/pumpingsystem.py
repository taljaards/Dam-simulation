import math
import pandas as pd
import logging, sys
logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)


class PumpingLevel:
    def __init__(self, name, capacity, initial_level, pump_flow, pump_power, pump_schedule_table, initial_pumps_status, fissure_water_inflow,
                 hysteresis=5.0, UL_LL=95.0, UL_HL=100.0, fed_to_level=None, pump_statuses_for_verification=None):
        self.name = name
        self.capacity = capacity
        self.pump_flow = pump_flow
        self.pump_power = pump_power
        self.pump_schedule_table = pump_schedule_table
        self.fissure_water_inflow = fissure_water_inflow
        self.level_history = [initial_level]
        self.pump_status_history = [initial_pumps_status]
        self.fed_to_level = fed_to_level  # to which level does this one pump?
        self.last_outflow = 0
        self.hysteresis = hysteresis
        self.UL_LL = UL_LL
        self.UL_HL = UL_HL
        self.UL_100 = False
        self.max_pumps = len([1 for r in pump_schedule_table if [150, 150, 150] not in r])
        self.pump_statuses_for_verification = pump_statuses_for_verification # this is only used in verification mode
        logging.info('{} pumping level created.'.format(self.name))

    def get_level_history(self, index=None):
        return self.level_history if index is None else self.level_history[index]

    # @levelHistory.setter
    def set_latest_level(self, value):
        self.level_history.append(value)

    def get_pump_status_history(self, index=None):
        return self.pump_status_history if index is None else self.pump_status_history[index]

    # @levelHistory.setter
    def set_latest_pump_status(self, value):
        self.pump_status_history.append(value)

    def get_scada_pump_schedule_table_level(self, pump_index, tariff_index):
        return self.pump_schedule_table[pump_index, tariff_index]

    def get_last_outflow(self):
        return 0 if self.fed_to_level is None else self.last_outflow

    def set_last_outflow(self, value):
        self.last_outflow = value

    def get_upstream_level_name(self):
        return self.fed_to_level

    def get_fissure_water_inflow(self, current_hour=None, current_minute=None, pumps=None):
        if isinstance(self.fissure_water_inflow, int) or isinstance(self.fissure_water_inflow, float):  # it is constant
            return self.fissure_water_inflow
        else:
            if self.fissure_water_inflow.shape[1] == 2:  # if 2 columns. Not f(pump)
                f1 = 0
                f2 = 1
                row = math.floor(current_hour)
            else:  # 3 columns. Is f(pump)
                f1 = 1
                f2 = 2
                row = pumps * 24 - 1 + math.floor(current_hour)

            if math.floor(current_minute) <= 30:
                col = f1
            else:
                col = f2

            return self.fissure_water_inflow[int(row), int(col)]

    def set_UL_100(self, bool_):
        self.UL_100 = bool_


def get_eskom_tou(current_hour):
    ch = current_hour
    if (7 <= ch < 10) or (18 <= ch < 20):  # Eskom peak
        tou_time_slot = 1
    elif (0 <= ch < 6) or (22 <= ch < 24):  # Eskom off-peak
        tou_time_slot = 3
    else:  # Eskom standard
        tou_time_slot = 2

    return tou_time_slot


def get_current_day_hour_minute(seconds):
    cd = math.floor(seconds / 86400)  # cd = current day
    ch = (seconds - cd * 86400) / (60 * 60)  # ch = current hour
    cm = (seconds - cd * 86400 - math.floor(ch) * 60 * 60) / 60  # cm = current minute

    return cd, ch, cm


class PumpSystem:
    def __init__(self, name):
        self.name = name
        self.levels = []
        self.eskom_tou = [3]
        self.total_power = []
        logging.info('{} pump system created.'.format(self.name))

    def add_level(self, pumping_level):
        self.levels.append(pumping_level)
        logging.info('{} pumping level added to {} pump system.'.format(pumping_level.name, self.name))

    def get_level_from_index(self, level_number):
        return self.levels[level_number]

    def get_level_from_name(self, level_name):
        for l in self.levels:
            if l.name == level_name:
                return l

    def __iter__(self):
        return iter(self.levels)

    def perform_simulation(self, mode, seconds=86400, save=False):
        # 86400 = seconds in one day
        logging.info('{} simulation started in {} mode.'.format(self.name, mode))

        if mode not in ['1-factor', '2-factor', 'verification']:
            raise ValueError('Invalid simulation mode specified')

        # reset simulation if it has run before
        if len(self.total_power) > 1:
            self.reset_pumpsystem_state()

        for t in range(1, seconds):  # start at 1, because initial conditions are specified
            _, ch, cm = get_current_day_hour_minute(t)

            tou_time_slot = get_eskom_tou(ch)
            self.eskom_tou.append(tou_time_slot)

            for level in self.levels:
                # scheduling algorithm
                if mode is not 'verification':
                    upstream_dam_name = level.get_upstream_level_name()
                    if mode == '1-factor' or upstream_dam_name is None:
                        upper_dam_level = 45
                    else:
                        upper_dam_level = self.get_level_from_name(upstream_dam_name).get_level_history(t - 1)

                    if upper_dam_level >= level.UL_HL:
                        level.set_UL_100(True)
                    if upper_dam_level <= level.UL_LL:
                        level.set_UL_100(False)

                    if not level.UL_100:
                        pumps_required = level.get_pump_status_history(t - 1)

                        do_next_check = False

                        for p in range(1, level.max_pumps + 1):
                            dam_level = level.get_level_history(t - 1)
                            pump_level = level.get_scada_pump_schedule_table_level(p - 1, tou_time_slot - 1)

                            if dam_level >= pump_level:
                                pumps_required_temp = p
                                do_next_check = True

                            if dam_level < (
                                level.get_scada_pump_schedule_table_level(0, tou_time_slot - 1) - level.hysteresis):
                                pumps_required = 0
                                do_next_check = False

                        if pumps_required >= (pumps_required_temp + 2):
                            pumps_required = pumps_required_temp + 1
                        if do_next_check:
                            if pumps_required_temp > pumps_required:
                                pumps_required = pumps_required_temp
                    else:
                        pumps_required = 0

                else:  # verification mode, so use actual statuses
                    pumps_required = level.pump_statuses_for_verification[t]

                # calculate and update simulation values
                pumps = pumps_required
                outflow = pumps * level.pump_flow

                level.set_last_outflow(outflow)

                additional_in_flow = 0
                for level2 in self.levels:
                    if level2.fed_to_level == level.name:
                        additional_in_flow += level2.get_last_outflow()

                level_new = level.get_level_history(t - 1) + 100 / level.capacity * (
                    level.get_fissure_water_inflow(ch, cm, pumps) + additional_in_flow - outflow)
                level.set_latest_level(level_new)
                level.set_latest_pump_status(pumps)

        # calculate pump system total power
        # can do it in the loop above, though
        power_list = []
        for level in self.levels:
            power_list.append(pd.DataFrame(level.get_pump_status_history()) * level.pump_power)
        self.total_power = pd.concat(power_list, axis=1).sum(axis=1).values

        logging.info('{} simulation completed in {} mode.'.format(self.name, mode))

        if save:
            self._save_simulation_results(mode, seconds)

    def _save_simulation_results(self, mode, seconds):
        df_list = []
        index = range(0, seconds)
        for level in self.levels:
            data_level = level.get_level_history()
            data_schedule = level.get_pump_status_history()
            data = {level.name + " Level": data_level,
                    level.name + " Status": data_schedule}
            df_list.append(pd.DataFrame(data=data, index=index))
        df = pd.concat(df_list, axis=1)

        data = {'Pump system total power': self.total_power,
                'Eskom ToU': self.eskom_tou}
        df = pd.concat([df, pd.DataFrame(data=data, index=index)], axis=1)
        df.index.name = 'seconds'
        df.to_csv('{}_simulation_data_export_{}.csv.gz'.format(self.name, mode), compression='gzip')
        logging.info('{} simulation data saved.'.format(mode))

    def reset_pumpsystem_state(self):
        self.eskom_tou = [3]
        self.total_power = []

        for level in self.levels:
            level.level_history = [level.level_history[0]]
            level.pump_status_history = [level.pump_status_history[0]]
            level.last_outflow = 0

        logging.info('{} pumping system successfully cleared.'.format(self.name))