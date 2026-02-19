
CREATE TYPE room_status AS ENUM ('Available', 'Dirty', 'Maintenance');
CREATE TYPE booking_status AS ENUM ('Pending', 'Confirmed', 'CheckedIn', 'Completed', 'Cancelled');


CREATE TABLE RoomTypes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    base_price DECIMAL(10, 2) NOT NULL,
    max_occupancy INT NOT NULL,
    description TEXT
);

CREATE TABLE Rooms (
    id SERIAL PRIMARY KEY,
    room_number VARCHAR(10) NOT NULL UNIQUE,
    room_type_id INT REFERENCES RoomTypes(id),
    status room_status DEFAULT 'Available'
);

CREATE TABLE Users (
    id SERIAL PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    phone VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Bookings (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES Users(id) NOT NULL,
    check_in_date DATE NOT NULL,
    check_out_date DATE NOT NULL,
    total_amount DECIMAL(10, 2),
    status booking_status DEFAULT 'Pending',
    CONSTRAINT valid_dates CHECK (check_out_date > check_in_date)
);


CREATE TABLE RoomAssignments (
    id SERIAL PRIMARY KEY,
    booking_id INT REFERENCES Bookings(id) ON DELETE CASCADE,
    room_id INT REFERENCES Rooms(id),
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


INSERT INTO RoomTypes (name, base_price, max_occupancy, description) VALUES
('Standard Double', 120.00, 2, 'Two twin beds, perfect for friends.'),
('Executive King', 250.00, 2, 'One king bed with a city view.'),
('Family Suite', 450.00, 4, 'Two bedrooms and a small kitchenette.');

INSERT INTO Rooms (room_number, room_type_id, status) VALUES
('101', 1, 'Available'),
('102', 1, 'Available'),
('201', 2, 'Available'),
('301', 3, 'Dirty');

INSERT INTO Users (full_name, email, phone) VALUES
('John Doe', 'john@example.com', '555-0199'),
('Jane Smith', 'jane@example.com', '555-0122');

INSERT INTO Bookings (user_id, check_in_date, check_out_date, total_amount, status) VALUES
(1, '2026-06-10', '2026-06-13', 360.00, 'Confirmed');

INSERT INTO RoomAssignments (booking_id, room_id) VALUES (1, 1);
