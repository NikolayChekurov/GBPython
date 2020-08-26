time = int(input("Введите время в секундах >>> "))
hrs = time // 3600
mins = (time - hrs * 3600) // 60
secs = time - (hrs * 3600 + mins * 60)
print(f"Время в человеческом формате - {hrs} : {mins} : {secs}")
